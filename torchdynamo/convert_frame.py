import functools
import itertools
import logging
import os
import traceback
import types
import typing
import weakref
from typing import Callable

import torch
from torch.fx.graph_module import _forward_from_src as original_forward_from_src

from . import config
from . import exc
from .allowed_functions import is_allowed
from .bytecode_analysis import remove_dead_code
from .bytecode_analysis import remove_pointless_jumps
from .bytecode_transformation import is_generator
from .bytecode_transformation import transform_code_object
from .eval_frame import TorchPatcher
from .eval_frame import WrapperBackend
from .eval_frame import always_optimize_code_objects
from .eval_frame import skip_code
from .exc import BackendCompilerFailed
from .exc import InternalTorchDynamoError
from .exc import TorchRuntimeError
from .exc import Unsupported
from .exc import unimplemented
from .guards import CheckFunctionManager
from .guards import GuardedCode
from .symbolic_convert import InstructionTranslator
from .utils import CleanupManager
from .utils import counters
from .utils import dynamo_timed
from .utils import filter_stack
from .utils import format_bytecode
from .utils import guard_failures
from .utils import init_logging
from .utils import is_namedtuple
from .utils import istype
from .utils import orig_code_map
from .utils import troubleshooting_url

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self):
        self.seen = []
        self.seen_ids = set()

    def add(self, strong_obj):
        idx = id(strong_obj)
        if idx not in self.seen_ids:
            obj = weakref.ref(strong_obj, lambda _: self.seen_ids.remove(idx))
            self.seen.append(obj)
            self.seen_ids.add(idx)

    def __contains__(self, item):
        return id(item) in self.seen_ids

    def clear(self):
        self.seen.clear()
        self.seen_ids.clear()


input_codes = Tracker()
output_codes = Tracker()


@functools.wraps(original_forward_from_src)
def fx_forward_from_src_skip_result(*args, **kwargs):
    # we monkey patch FX to prevent infinite loop of trying to convert
    # our generated code
    result: types.FunctionType = original_forward_from_src(*args, **kwargs)
    skip_code(result.__code__)
    return result


def wrap_compiler_fn(compiler_fn):
    """WrapperBackend if config.verify_correctness is True"""
    if config.verify_correctness:
        # wrap backend if verify_correctness is True
        wrapper_backend_compiler_fn = WrapperBackend(compiler_fn)

        wrapper_backend_compiler_fn._torchdynamo_orig_callable = compiler_fn
        return wrapper_backend_compiler_fn

    return compiler_fn


def wrap_convert_context(fn):
    """
    Context manager to:
        1) Save/restore torch random state
        2) Save/restore torch.is_grad_enabled() state
        3) Monkey patch torch.fx.graph_module._forward_from_src
    """

    @functools.wraps(fn)
    def _fn(*args, **kwargs):
        prior_grad_mode = torch.is_grad_enabled()
        rng_state = torch.random.get_rng_state()
        if torch.cuda.is_available():
            cuda_rng_state = torch.cuda.get_rng_state()
        prior_fwd_from_src = torch.fx.graph_module._forward_from_src
        torch.fx.graph_module._forward_from_src = fx_forward_from_src_skip_result
        try:
            return fn(*args, **kwargs)
        finally:
            torch._C._set_grad_enabled(prior_grad_mode)
            torch.random.set_rng_state(rng_state)
            if torch.cuda.is_available():
                torch.cuda.set_rng_state(cuda_rng_state)
            torch.fx.graph_module._forward_from_src = prior_fwd_from_src

    _fn._torchdynamo_orig_callable = fn
    return _fn


@TorchPatcher.suppress_torch_distributed_warnings
def has_tensor_in_frame(frame):
    """Check if the frame has torch.* related bits"""
    # Check if the function was decorated using torchdynamo.optimize
    if frame.f_code in always_optimize_code_objects:
        return True

    # Check if there is global import of torch.*
    for co_name in frame.f_code.co_names:
        if co_name in frame.f_globals:
            if is_allowed(frame.f_globals[co_name]):
                return True

    seen_ids = dict()

    def has_tensor(obj):
        """Recursively check if the obj has a tensor"""
        obj_id = id(obj)
        if obj_id in seen_ids:
            return seen_ids[obj_id]
        seen_ids[obj_id] = False

        if isinstance(obj, (torch.Tensor, torch.nn.Module)):
            seen_ids[obj_id] = True
            return seen_ids[obj_id]
        elif istype(obj, (list, tuple)):
            seen_ids[obj_id] = any([has_tensor(v) for v in obj])
            return seen_ids[obj_id]
        elif istype(obj, dict):
            seen_ids[obj_id] = any([has_tensor(v) for v in obj.values()])
            return seen_ids[obj_id]
        elif istype(obj, (str, int, float, type(None), bool)):
            seen_ids[obj_id] = False
            return seen_ids[obj_id]
        elif is_namedtuple(obj):
            seen_ids[obj_id] = any([has_tensor(getattr(obj, v)) for v in obj._fields])
            return seen_ids[obj_id]
        elif (
            not is_allowed(obj)
            and hasattr(obj, "__dict__")
            and len(getattr(obj, "__dict__"))
        ):
            seen_ids[obj_id] = any([has_tensor(v) for v in obj.__dict__.values()])
            return seen_ids[obj_id]
        else:
            # if config.debug:
            #     print(
            #         f"Assuming that object of type {type(obj)} does not have a tensor"
            #     )
            return False

    # Check if the passed arguments are of type Tensor
    for value in frame.f_locals.values():
        if has_tensor(value):
            return True

    log.debug(
        f"skipping because no torch.* {frame.f_code.co_name} \
            {frame.f_code.co_filename} {frame.f_code.co_firstlineno}"
    )

    return False


def format_error_msg(exc, frame):
    msg = os.linesep * 2
    if config.verbose:
        msg = format_bytecode("WON'T CONVERT", frame, frame.f_code)
        msg += "=" * 10 + " TorchDynamo Stack Trace " + "=" * 10 + "\n"
        msg += traceback.format_exc()
        if hasattr(exc, "real_stack"):
            msg += (
                "\n"
                + "=" * 10
                + " The above exception occurred while processing the following code "
                + "=" * 10
                + "\n\n"
            )
            msg += "".join(
                traceback.format_list(
                    filter_stack(traceback.extract_stack(frame))
                    + list(reversed(exc.real_stack))
                )
            )
    else:
        msg = f"WON'T CONVERT {frame.f_code.co_name} {frame.f_code.co_filename}\
 line {frame.f_code.co_firstlineno} \ndue to: \n{traceback.format_exc(limit=-1)}"

        if hasattr(exc, "real_stack"):
            msg += f"\nfrom user code:\n {''.join(traceback.format_list([exc.real_stack[-1]]))}"

        msg += "\nSet torchdynamo.config.verbose=True for more information\n"
    msg += "=" * 10
    return msg


def convert_frame_assert(
    compiler_fn: Callable, guard_export_fn=None, one_graph=True, export=False
):
    """Fully convert a frame into an FX graph"""
    init_logging()

    compiler_fn = wrap_compiler_fn(compiler_fn)

    @dynamo_timed
    def _convert_frame_assert(frame: types.FrameType, cache_size: int):
        code = frame.f_code
        input_codes.add(code)
        if code in output_codes:
            return None
        if (
            os.environ.get("TORCHDYNAMO_DEBUG_FUNCTION")
            and os.environ.get("TORCHDYNAMO_DEBUG_FUNCTION") != code.co_name
        ):
            return None
        if code.co_name == "<genexpr>" and code.co_filename.endswith(
            ("transformers/file_utils.py", "transformers/utils/generic.py")
        ):
            # not needed, but cleans up torchbench error stats
            return None
        if code.co_name == "__setattr__":
            # setattr could be tricky to handle generally,
            # but also not likely useful to compile- skip the whole frame
            return None
        # Check if the frame is generated by an exec builtin call
        # TODO - Running exec generated frame seems propagates f_globals to the
        # next frames.
        if code.co_name == "<module>" and code.co_filename == "<string>":
            return None

        if (
            code.co_name == "<lambda>"
            and code.co_filename == "<string>"
            and not bool(frame.f_builtins)
        ):
            # namedtuple subclass constructor. Empty builtins cause issue with
            # len keyword in LIST_LEN guard.
            return None

        if is_generator(code):
            unimplemented("generator")
        if cache_size >= config.cache_size_limit:

            def format_func_info(code):
                return f"'{code.co_name}' ({code.co_filename}:{code.co_firstlineno})"

            def format_guard_failures(code):
                # For the common case, it's sufficient to see just the most recent failure.
                # We could add a verbose mode if needed
                return f"{str(guard_failures[code][-1])}"

            assert code in guard_failures, "TODO(whc) any other recompile reasons?"
            log.warning(
                f"torchdynamo hit config.cache_size_limit ({config.cache_size_limit})\n"
                + f"   function: {format_func_info(code)}\n"
                + f"   reasons:  {format_guard_failures(code)}\n"
                + f"to diagnose recompilation issues, see {troubleshooting_url}."
            )
            unimplemented("cache_size_limit reached")
        output = None

        if not has_tensor_in_frame(frame):
            return None

        # from .utils import print_once;  print_once(code.co_filename)

        def transform(instructions, code_options):
            nonlocal output
            tracer = InstructionTranslator(
                instructions,
                frame.f_code,
                frame.f_locals,
                frame.f_globals,
                frame.f_builtins,
                code_options,
                compiler_fn,
                one_graph,
                export,
            )
            tracer.run()
            output = tracer.output
            assert output.output_instructions
            instructions[:] = output.output_instructions
            code_options.update(output.code_options)

            if config.dead_code_elimination:
                instructions[:] = remove_pointless_jumps(remove_dead_code(instructions))

        try:
            for attempt in itertools.count():
                try:
                    code = transform_code_object(frame.f_code, transform)
                    orig_code_map[code] = frame.f_code
                    break
                except exc.RestartAnalysis:
                    log.debug("Restarting analysis ...")
                    if attempt > 100:
                        unimplemented("100+ RestartAnalysis() calls")
                except exc.SkipFrame:
                    log.debug(
                        f"Skipping frame {frame.f_code.co_name} \
                        {frame.f_code.co_filename} {frame.f_code.co_firstlineno}"
                    )
                    if one_graph:
                        log.debug("No graph captured with one_graph=True")
                    return None
            output_codes.add(code)

            log.info(format_bytecode("ORIGINAL BYTECODE", frame, frame.f_code))
            log.info(format_bytecode("MODIFIED BYTECODE", frame, code))

            assert output.guards is not None
            CleanupManager.instance[code] = output.cleanups
            check_fn = CheckFunctionManager(
                output.guards, frame.f_locals, frame.f_globals
            )

            guarded_code = GuardedCode(code, check_fn.check_fn)
            guard_str = "GUARDS:\n"
            guard_str += "\n".join(
                [f" - {str(guard)}" for guard in sorted(output.guards)]
            )

            log.info(guard_str)

            if guard_export_fn is not None:
                guard_export_fn(output.guards)

            return guarded_code
        except (
            Unsupported,
            TorchRuntimeError,
            BackendCompilerFailed,
            AssertionError,
        ) as e:
            log.error(format_error_msg(e, frame))
            raise
        except Exception as e:
            log.error(format_error_msg(e, frame))
            raise InternalTorchDynamoError()

    _convert_frame_assert._torchdynamo_orig_callable = compiler_fn
    return wrap_convert_context(_convert_frame_assert)


def convert_frame(compiler_fn: typing.Callable, guard_export_fn=None):
    """Try to convert a frame into an FX graph, if error leave frame unmodified"""
    inner_convert = convert_frame_assert(compiler_fn, guard_export_fn, one_graph=False)

    def _convert_frame(frame: types.FrameType, cache_size: int):
        counters["frames"]["total"] += 1
        try:
            result = inner_convert(frame, cache_size)
            counters["frames"]["ok"] += 1
            return result
        except AssertionError:
            if config.raise_on_assertion_error:
                raise
        except BackendCompilerFailed:
            raise
        except Exception:
            pass
        return None

    _convert_frame._torchdynamo_orig_callable = compiler_fn
    return _convert_frame
