#!/usr/bin/env pytest
import os
from unittest.mock import patch

import torch
import torch.distributed as dist
from torch import nn
from torch.nn.parallel import DistributedDataParallel as DDP

import torchdynamo
from torchdynamo import config
from torchdynamo.testing import same


class ToyModel(nn.Module):
    def __init__(self, in_feat=10, hidden_feat=5000, num_hidden=2, out_feat=5):
        super().__init__()
        self.net = nn.Sequential(
            *[nn.Linear(in_feat, hidden_feat), nn.ReLU()]
            + [nn.Linear(5000, 5000), nn.ReLU()] * num_hidden
            + [nn.Linear(5000, 5), nn.ReLU()]
        )

    def forward(self, inputs):
        return self.net(inputs)


class CheckSplitsCompiler:
    def __init__(self):
        self.compiler_called = 0

    def compile_fn(self, gm, example_inputs):
        self.compiler_called += 1
        return gm


class TestDDPOptimizer(torchdynamo.testing.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # _exit_stack is set up in TestCase
        cls._exit_stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "MASTER_ADDR": "localhost",
                    "MASTER_PORT": "12355",
                },
            )
        )
        cls._exit_stack.enter_context(patch.object(config, "optimize_ddp", True))
        cls.rank = 0
        cls.device = f"cuda:{cls.rank}"
        dist.init_process_group("nccl", rank=cls.rank, world_size=1)

    @classmethod
    def tearDownClass(cls):
        dist.destroy_process_group()
        super().tearDownClass()

    def get_model(self):
        m = ToyModel().to(self.device)
        inputs = torch.randn(20, 10).to(self.device)
        outputs = m(inputs)
        return m, inputs, outputs

    def test_graph_split(self):
        """
        Just ensures that the appropriate number of splits happen (based on
        bucket size and model parameters) - verifies the number of times
        the user-provided compiler is called by the DDPOptimizer which is
        doing the graph splitting
        """
        m, inputs, correct_outputs = self.get_model()
        ddp_m = DDP(m, device_ids=[self.rank], bucket_cap_mb=25)

        check_splits_compiler = CheckSplitsCompiler()

        @torchdynamo.optimize(check_splits_compiler.compile_fn)
        def opt_fn(inputs):
            return ddp_m(inputs)

        opt_outputs = opt_fn(inputs)
        self.assertTrue(same(correct_outputs, opt_outputs))
        self.assertEqual(check_splits_compiler.compiler_called, 3)

    def test_no_split(self):
        """
        Ensures the DDPOptimizer returns a correct, compiled module without
        introducing graph splits. (Based on model parmeters fitting in the bucket)
        """
        m, inputs, correct_outputs = self.get_model()
        ddp_m = DDP(m, device_ids=[self.rank], bucket_cap_mb=250)

        check_splits_compiler = CheckSplitsCompiler()

        @torchdynamo.optimize(check_splits_compiler.compile_fn)
        def opt_fn(inputs):
            return ddp_m(inputs)

        opt_outputs = opt_fn(inputs)
        self.assertTrue(same(correct_outputs, opt_outputs))
        self.assertEqual(check_splits_compiler.compiler_called, 1)

    def test_aot_autograd(self):
        """
        Explicitly check AotAutograd family of compilers work,
        since they require example inputs propagated between graph splits.
        """
        m, inputs, correct_outputs = self.get_model()
        ddp_m = DDP(m, device_ids=[self.rank], bucket_cap_mb=25)

        @torchdynamo.optimize("aot_nvfuser")
        def opt_fn(inputs):
            return ddp_m(inputs)

        opt_outputs = opt_fn(inputs)
        opt_outputs.sum().backward()
        self.assertTrue(same(correct_outputs, opt_outputs))
