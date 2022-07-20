import contextlib
import copy
import functools
import logging
import threading
import warnings

import torch

from torchdynamo.utils import checkpoint_params
from torchdynamo.utils import clone_inputs
from torchdynamo.utils import recursive_allclose

from . import config
from . import convert_frame
from . import skipfiles
from . import utils
from .mutation_guard import install_generation_tagging_init
from .utils import same

log = logging.getLogger(__name__)

try:
    from . import _eval_frame
except (ModuleNotFoundError, ImportError) as e:
    raise RuntimeError("run `python setup.py develop` to compile C extensions") from e

try:
    from torch.fx.experimental import proxy_tensor
except (ModuleNotFoundError, ImportError):
    proxy_tensor = None

set_eval_frame = _eval_frame.set_eval_frame
reset_code = _eval_frame.reset_code
unsupported = _eval_frame.unsupported
skip_code = _eval_frame.skip_code
set_guard_fail_hook = _eval_frame.set_guard_fail_hook
set_guard_error_hook = _eval_frame.set_guard_error_hook

always_optimize_code_objects = utils.ExactWeakKeyDictionary()


def nothing():
    pass


null_context = contextlib.nullcontext

unset = object()

compile_lock = threading.Lock()


class _TorchDynamoContext:
    def __init__(
        self,
        callback,
        on_enter=nothing,
        backend_ctx_ctor=null_context,
        patch_fn=nothing,
    ):
        super().__init__()
        assert callable(callback) or callback is False or callback is None
        self.callback = callback
        self.prior = unset
        self.on_enter = on_enter
        self.extra_ctx_ctor = backend_ctx_ctor
        patch_fn()

    def __enter__(self):
        self.on_enter()
        self.prior = set_eval_frame(self.callback)
        self.backend_ctx = self.extra_ctx_ctor()
        self.backend_ctx.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        set_eval_frame(self.prior)
        self.prior = unset
        self.backend_ctx.__exit__(exc_type, exc_val, exc_tb)

    def __call__(self, fn):
        assert callable(fn)
        callback = self.callback
        on_enter = self.on_enter
        backend_ctx_ctor = self.extra_ctx_ctor

        @functools.wraps(fn)
        def _fn(*args, **kwargs):
            on_enter()
            prior = set_eval_frame(callback)
            backend_ctx = backend_ctx_ctor()
            backend_ctx.__enter__()
            try:
                return fn(*args, **kwargs)
            finally:
                set_eval_frame(prior)
                backend_ctx.__exit__()

        # hooks to properly handle inlining
        if isinstance(self, DisableContext):
            _fn._torchdynamo_disable = True
        else:
            _fn._torchdynamo_inline = fn

        # If the function is called with torchdynamo.optimize decorator, we
        # should prevent any type of skipping.
        if callback not in (None, False):
            always_optimize_code_objects[fn.__code__] = True

        return _fn


class OptimizeContext(_TorchDynamoContext):
    def __init__(self, callback, backend_ctx_ctor):
        super().__init__(
            callback=callback,
            on_enter=install_generation_tagging_init,
            backend_ctx_ctor=backend_ctx_ctor,
            patch_fn=TorchPatcher.patch,
        )


class RunOnlyContext(_TorchDynamoContext):
    def __init__(self):
        super().__init__(callback=False)


class DisableContext(_TorchDynamoContext):
    def __init__(self):
        super().__init__(callback=None)


def catch_errors_wrapper(callback):
    @functools.wraps(callback)
    def catch_errors(frame, cache_size):
        try:
            if frame.f_lasti >= 0 or skipfiles.check(frame.f_code.co_filename):
                if config.debug:
                    print(f"skipping {frame.f_code.co_name} {frame.f_code.co_filename}")
                return None
            if (
                frame.f_code.co_filename == "<string>"
                and frame.f_code.co_name == "__new__"
            ):
                # nametuple constructor
                return None
            with compile_lock:
                return callback(frame, cache_size)
        except Exception:
            logging.basicConfig()
            logging.exception("Error while processing frame")
            raise

    return catch_errors


def _optimize_catch_errors(compile_fn, backend_ctx_ctor=null_context):
    return OptimizeContext(
        catch_errors_wrapper(compile_fn), backend_ctx_ctor=backend_ctx_ctor
    )


class WrapperBackend:
    def __init__(self, backend=None):
        self.backend = backend

    @property
    def example_inputs(self):
        return clone_inputs(self.original_example_inputs)

    def __call__(self, gm: torch.fx.GraphModule, example_inputs):

        self.restore = checkpoint_params(gm)
        self.original_example_inputs = clone_inputs(example_inputs)
        self.gm = gm
        copy_gm = copy.deepcopy(self.gm)
        self.candidate = self.backend(copy_gm, self.original_example_inputs)

        if self.candidate is None or self.candidate is self.gm.forward:
            return self.gm.forward

        if not config.verify_correctness:
            return self.candidate

        # if verify_correctness=True
        try:
            correct = self.gm.forward(*self.example_inputs)
            result = self.candidate(*self.example_inputs)

            # TODO: replace `same` function with the one in testing
            if same(correct, result):
                return self.candidate

            raise RuntimeError(f"incorrect results of backend {self}")
            return self.gm.forward

        except Exception:
            log.exception("error in verify_correctness")
            raise
        finally:
            self.restore()


def optimize(backend, nopython=False):
    """
    The main entrypoint of TorchDynamo.  Do graph capture and call
    backend() to optimize extracted graphs.

    Args:
        backend: One of the two things:
            - Either, a function/callable taking a torch.fx.GraphModule and
            example_inputs and returning a python callable that runs the
            graph faster.
            One can also provide additional context for the backend, like
            torch.jit.fuser("fuser2"), by setting the backend_ctx_ctor attribute.
            See AOTAutogradMemoryEfficientFusionWithContext for the usage.
            - Or, a string backend name in `torchdynamo.list_backends()`
        nopython: If True, graph breaks will be errors and there will
            be a single whole-program graph.

    Example Usage:

        @torchdynamo.optimize("ofi")
        def toy_example(a, b):
            ...

        or

        with torchdynamo.optimize(my_compiler):
           ...
    """
    backend_ctx_ctor = null_context
    if hasattr(backend, "backend_ctx_ctor"):
        backend_ctx_ctor = getattr(backend, "backend_ctx_ctor")

    if nopython:
        return optimize_assert(backend, backend_ctx_ctor, guard_export_fn=None)
    return _optimize_catch_errors(
        convert_frame.convert_frame(backend, guard_export_fn=None), backend_ctx_ctor
    )


def export(f, *args, **kwargs):
    import torch.utils._pytree as pytree

    graph = None
    out_guards = None
    compiler_captured_inputs = None

    def assert_can_rewrite_spec(
        source_args, candidate_args, new_spec, original_spec, target_result
    ):
        matched_elements_positions = []
        for x in candidate_args:
            matched_elements = [torch.allclose(x, y) for y in source_args]
            matched_elements = set(
                matched_elements.index(True) for x in matched_elements
            )
            matched_elements_positions.extend(matched_elements)

        reconstructed = pytree.tree_unflatten(
            [source_args[i] for i in matched_elements_positions], new_spec
        )

        if reconstructed is None or not recursive_allclose(
            reconstructed, target_result
        ):
            assert False, f"Spec mismatch: {original_spec}, {new_spec}."

    def guard_export_print(guards):
        nonlocal out_guards
        assert out_guards is None, "whole graph export entails exactly one guard export"
        out_guards = guards

    def dynamo_normalization_capturing_compiler(
        gm: torch.fx.GraphModule, example_inputs
    ):
        nonlocal graph
        nonlocal compiler_captured_inputs

        assert graph is None, "whole graph export entails exactly one graph"
        graph = gm
        compiler_captured_inputs = example_inputs
        return gm.forward

    backend_ctx_ctor = null_context

    result_traced = None
    with optimize_assert(
        dynamo_normalization_capturing_compiler, backend_ctx_ctor, guard_export_print
    ):
        result_traced = f(*args, **kwargs)

    assert graph is not None, "whole graph export entails exactly one call"
    assert out_guards is not None, "whole graph export entails exactly one guard export"

    # TODO(voz): Handle kwargs properly?
    flat_args, in_spec = pytree.tree_flatten(args)
    dynamo_flat_args, dynamo_in_spec = pytree.tree_flatten(compiler_captured_inputs)

    if in_spec != dynamo_in_spec:
        if len(dynamo_flat_args) > 0:
            assert_can_rewrite_spec(
                dynamo_flat_args, flat_args, in_spec, dynamo_in_spec, args
            )

    flat_results_traced, out_spec_traced = pytree.tree_flatten(result_traced)

    result_export = graph.forward(*flat_args)
    flat_results_export, out_spec_export = pytree.tree_flatten(result_export)

    flat_inputs_to_exported_graph, flat_inputs_spec = pytree.tree_flatten(
        compiler_captured_inputs
    )

    if out_spec_export != out_spec_traced:
        flat_both = flat_inputs_to_exported_graph + flat_results_export
        assert_can_rewrite_spec(
            flat_both,
            flat_results_traced,
            out_spec_traced,
            out_spec_export,
            result_traced,
        )

    # TODO(voz): call _codegen on the graph to rewrite the graphs input and output
    # Blocked on bugs in fx. https://github.com/pytorch/pytorch/pull/81177
    # graph.graph._codegen = _PyTreeCodeGen(
    # _PyTreeInfo(
    #         [f"orig_arg_{i}" for i in range(len(inputs))],
    #         in_spec,
    #         out_spec_traced,
    #     )
    # )
    # graph.recompile()

    # TODO(voz): A major feature gap here, atm, is that we return a graph with a flat_args signature.
    # There is currently more work that needs to be done on the fx side before we can support the UX we want.
    # The future UX here will not return a spec, but will rather return a graph with the original signature
    # and return type as the passed in callable, `f`.
    return (graph, out_guards, in_spec, out_spec_traced)


def optimize_assert(backend, backend_ctx_ctor=null_context, guard_export_fn=None):
    """
    The same as `torchdynamo.optimize(backend, nopython=True)`
    """
    return _optimize_catch_errors(
        convert_frame.convert_frame_assert(backend, guard_export_fn), backend_ctx_ctor
    )


def run(fn=None):
    """Don't do any dynamic compiles, just use prior optimizations"""
    if fn is not None:
        assert callable(fn)
        return RunOnlyContext()(fn)
    return RunOnlyContext()


def disable(fn=None):
    """Decorator and context manager to disable TorchDynamo"""
    if fn is not None:
        assert callable(fn)
        return DisableContext()(fn)
    return DisableContext()


def skip(fn=None):
    """
    Skip frames associated with the function code, but still process recursively
    invoked frames
    """
    if fn is None:
        return skip
    assert callable(fn)
    skip_code(fn.__code__)
    fn._torchdynamo_disable = True
    return fn


class TorchPatcher:
    @staticmethod
    @functools.lru_cache(None)
    def patch():
        # Disable TorchDynamo on some torch.* compilers generated frames
        torch.jit.trace = disable(torch.jit.trace)
        torch.jit.trace_module = disable(torch.jit.trace_module)
        torch.jit._get_trace_graph = disable(torch.jit._get_trace_graph)

        # symbolic_trace creates new frames. We disable Dynamo on such frames
        torch.fx._symbolic_trace.Tracer.trace = disable(
            torch.fx._symbolic_trace.Tracer.trace
        )

        torch.onnx.export_to_pretty_string = disable(torch.onnx.export_to_pretty_string)
        torch.distributions.Distribution.set_default_validate_args(False)

        if proxy_tensor is not None:
            proxy_tensor.dispatch_trace = disable(proxy_tensor.dispatch_trace)

    @staticmethod
    def suppress_torch_distributed_warnings(fn):
        def inner_fn(*args, **kwargs):
            warnings.filterwarnings(
                "ignore", category=UserWarning, module="torch.distributed"
            )
            return fn(*args, **kwargs)

        return inner_fn
