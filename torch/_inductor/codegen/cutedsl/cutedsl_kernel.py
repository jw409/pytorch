# mypy: allow-untyped-defs
import contextlib
import dataclasses
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, TYPE_CHECKING

import torch
from torch._inductor import config
from torch._inductor.codegen.common import IndentedBuffer, KernelTemplate, Kernel
from torch._inductor.ir import Buffer, CuteDSLTemplateBuffer, IRNode, Layout, TensorBox, ExternKernel
from torch._inductor.select_algorithm import PartialRender
from torch._inductor.utils import OrderedSet
from torch._inductor.virtualized import V

if TYPE_CHECKING:
    from .cutedsl_template import CuteDSLBenchmarkRequest, CuteDSLTemplate

log = logging.getLogger(__name__)


class SimpleCuteDSLCSE:
    """Simplified CSE for CuteDSL kernels."""
    
    def __init__(self):
        pass
    
    def invalidate(self, vars_to_invalidate):
        """No-op invalidation for CuteDSL - templates handle their own optimization."""
        pass


@dataclasses.dataclass
class CuteDSLSubgraphInfo:
    """Minimal subgraph info for CuteDSL kernels."""
    body: IndentedBuffer
    template_mask: Optional[str] = None
    template_out: Optional[str] = None

    def to_dict(self):
        return {
            field.name: getattr(self, field.name) for field in dataclasses.fields(self)
        }


class CuteDSLKernel(Kernel):
    """
    Base class for CuteDSL (CUTLASS Python DSL) kernels.
    Follows the same pattern as CUDAKernel, ROCmKernel, etc.
    Provides CuteDSL-specific functionality for tensor conversion and kernel configuration.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # CuteDSL-specific attributes
        self.cute_tensors: dict[str, Any] = {}
        self.kernel_config: dict[str, Any] = {}
        self.grid_config: Optional[tuple] = None
        
        # Layout and tensor management for CuteDSL
        self.tensor_layouts: dict[str, Any] = {}
        self.cutlass_args: list[Any] = []
        
    def create_cute_tensor_spec(self, buffer: Buffer, name: str) -> dict[str, Any]:
        """
        Convert PyTorch buffer to CuTe tensor specification.
        
        Args:
            buffer: PyTorch inductor buffer
            name: Name for the tensor in CuteDSL context
            
        Returns:
            Dictionary containing tensor specification for CuteDSL
        """
        layout = buffer.get_layout()
        
        spec = {
            'name': name,
            'shape': layout.size,
            'stride': layout.stride if hasattr(layout, 'stride') else None,
            'dtype': layout.dtype,
            'device': layout.device,
        }
        
        self.cute_tensors[name] = spec
        return spec
        
    def configure_kernel_launch(self, grid_config: tuple, block_config: Optional[tuple] = None):
        """
        Configure CuteDSL kernel launch parameters.
        
        Args:
            grid_config: Grid dimensions for kernel launch
            block_config: Block dimensions (optional)
        """
        self.grid_config = grid_config
        self.kernel_config['grid'] = grid_config
        if block_config:
            self.kernel_config['block'] = block_config
            
    def add_cutlass_arg(self, arg_name: str, arg_value: Any):
        """Add argument for CUTLASS kernel execution."""
        self.cutlass_args.append((arg_name, arg_value))
        
    def get_cute_tensor_conversion_code(self, buffer_name: str, target_name: str) -> str:
        """
        Generate code to convert PyTorch tensor to CuTe tensor format.
        
        Args:
            buffer_name: Name of the PyTorch buffer
            target_name: Name for the CuTe tensor
            
        Returns:
            Code string for tensor conversion
        """
        return f"{target_name} = cute.tensor_from_dlpack({buffer_name})"


class CuteDSLTemplateKernel(CuteDSLKernel):
    """
    Template kernel implementation for CuteDSL (CUTLASS Python DSL).
    Handles code generation and argument management for CuteDSL CUDA kernels.
    Inherits from CuteDSLKernel to provide proper template infrastructure.
    """

    def __init__(
        self,
        kernel_name: str,
        input_nodes: list[Buffer],
        output_node: Buffer,
    ) -> None:
        # Call parent CuteDSLKernel constructor (which calls Kernel constructor)
        super().__init__()
        self.kernel_name = kernel_name
        self.input_nodes = input_nodes
        self.output_node = output_node
        
        # Subgraph management for template processing
        self.subgraph_bodies: dict[str, CuteDSLSubgraphInfo] = {}
        
        # Template attributes
        self.body: IndentedBuffer = IndentedBuffer()
        self.template_mask: Optional[str] = None
        self.template_out: Optional[str] = None
        self.template_indices: Optional[list] = None
        self.render_hooks: dict[str, Any] = {}
        
        # Additional attributes needed by template system
        self.prologue_fused_inputs: OrderedSet[str] = OrderedSet()
        self.prologue_fused_inputs_preserve_zero: OrderedSet[str] = OrderedSet()
        self.named_input_nodes: dict[str, Buffer] = {}
        
        # Create named input nodes mapping
        for i, input_node in enumerate(input_nodes):
            node_name = getattr(input_node, 'name', f'input_{i}')
            self.named_input_nodes[node_name] = input_node
            
        # CSE (Common Subexpression Elimination) - simplified for CuteDSL
        self.cse = SimpleCuteDSLCSE()

    def render(self, template, **kwargs):
        """Render the kernel using the template, returning rendered code string with placeholders."""
        # Create template environment with functions that can be called from templates
        template_env = {
            'store_output': self.store_output,
            'def_kernel': self.def_kernel,
            # Add other template functions as needed
        }
        
        # Render the template with the environment and provided kwargs
        rendered_code = template.render(
            kernel_name=self.kernel_name,
            input_nodes=self.input_nodes,
            output_node=self.output_node,
            **template_env,
            **kwargs
        )
        
        # Return the rendered code string with placeholders (hooks registered in self.render_hooks)
        return rendered_code

    def __enter__(self):
        """Context manager entry - CuteDSL doesn't need complex setup."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - CuteDSL doesn't need complex cleanup."""
        pass

    @contextlib.contextmanager
    def set_subgraph_body(self, body_name: str):
        """Set the active subgraph body for template processing."""
        assert all(
            hasattr(self, field.name) for field in dataclasses.fields(CuteDSLSubgraphInfo)
        )
        old_state = {
            key.name: getattr(self, key.name)
            for key in dataclasses.fields(CuteDSLSubgraphInfo)
        }

        # Auto-create subgraph if it doesn't exist (for kernels without epilogue fusion)
        if body_name not in self.subgraph_bodies:
            self.subgraph_bodies[body_name] = CuteDSLSubgraphInfo(
                body=IndentedBuffer(),
                template_mask=None,
                template_out=None,
            )

        subgraph = self.subgraph_bodies[body_name]
        for key, value in subgraph.to_dict().items():
            setattr(self, key, value)

        try:
            yield
        finally:
            # Save current state back to subgraph
            self.subgraph_bodies[body_name] = CuteDSLSubgraphInfo(
                **{
                    key.name: getattr(self, key.name)
                    for key in dataclasses.fields(CuteDSLSubgraphInfo)
                }
            )
            # Restore old state
            for key, value in old_state.items():
                setattr(self, key, value)

    @contextlib.contextmanager
    def create_subgraph_body(self, body_name: str):
        """Create a new subgraph body for template processing."""
        assert body_name not in self.subgraph_bodies, f"Subgraph body '{body_name}' already exists"
        self.subgraph_bodies[body_name] = CuteDSLSubgraphInfo(
            body=IndentedBuffer(),
            template_mask=None,
            template_out=None,
        )
        with self.set_subgraph_body(body_name):
            yield

    def split_and_set_ranges(self, ranges):
        """Split and set ranges for template processing. For CuteDSL, just return ranges as-is."""
        return ranges

    def estimate_kernel_num_bytes(self) -> float:
        """Estimate kernel memory usage in bytes. Placeholder for CuteDSL."""
        return 0.0

    def imports_for_benchmark_kernel(self) -> str:
        """Generate imports needed for benchmarking. Placeholder for CuteDSL."""
        return ""

    def codegen_kernel_benchmark(self, num_gb: float) -> IndentedBuffer:
        """Generate benchmark code. Placeholder for CuteDSL."""
        return IndentedBuffer()

    def store_output(
        self,
        indices,
        val: str,
        mask: Optional[str] = None,  
        indent_width: int = 4,
    ):
        """Store output for CuteDSL templates. Simplified version of TritonTemplateKernel.store_output."""
        # Create the <STORE_OUTPUT> subgraph
        with self.create_subgraph_body("<STORE_OUTPUT>"):
            # Set template attributes
            self.template_mask = mask
            self.template_out = val
            self.template_indices = list(indices) if indices else []
            
            # For CuteDSL, just add a simple comment to the body
            self.body.writeline(f"# Store output: {val}")
            if mask:
                self.body.writeline(f"# Mask: {mask}")

        # Create a simple render hook that returns the body content
        def hook():
            self.cse.invalidate(OrderedSet())
            return self.body.getvalue().strip()

        # Register the render hook
        assert "<STORE_OUTPUT>" not in self.render_hooks
        self.render_hooks["<STORE_OUTPUT>"] = hook
        
        # Return the placeholder string that will be replaced by the hook
        return "<STORE_OUTPUT>"

    def def_kernel(self, *argnames):
        """Define kernel function signature for CuteDSL templates."""
        # Populate self.args with input/output buffer mappings like other template kernels do
        if len(argnames) > 0:
            # Map argument names to input nodes
            for i, (name, input_node) in enumerate(zip(argnames, self.input_nodes)):
                self.named_input_nodes[name] = input_node
                # Populate the args.input_buffers mapping (this is key!)
                self.args.input_buffers[input_node.get_name()] = name
        
        # Map output node if present
        if self.output_node:
            output_name = "output"  # Default output name
            self.args.output_buffers[self.output_node.get_name()] = output_name
        
        # Create the <DEF_KERNEL> subgraph to satisfy Triton scheduler expectations
        with self.create_subgraph_body("<DEF_KERNEL>"):
            # Generate CuteDSL kernel definition code
            if argnames:
                args_str = ', '.join(argnames)
                self.body.writeline(f"# CuteDSL kernel function: {self.kernel_name}")
                self.body.writeline(f"# Arguments: {args_str}")
                self.body.writeline(f"def {self.kernel_name}({args_str}):")
            else:
                self.body.writeline(f"# CuteDSL kernel function: {self.kernel_name}")
                self.body.writeline(f"def {self.kernel_name}():")
        
        # Create render hook that returns the generated kernel definition
        def hook():
            self.cse.invalidate(OrderedSet())
            return self.body.getvalue().strip()
        
        # Register the render hook (required by Triton scheduler)
        assert "<DEF_KERNEL>" not in self.render_hooks, "DEF_KERNEL hook already registered"
        self.render_hooks["<DEF_KERNEL>"] = hook
        
        # Return the placeholder string that will be replaced by the hook
        return "<DEF_KERNEL>"

    def call_kernel(self, name: str, node=None):
        """Generate code to call the CuteDSL kernel (Python implementation)."""
        wrapper = V.graph.wrapper_code
        
        # CuteDSL kernels are Python functions, so we need Python tensor arguments
        if hasattr(self, 'args') and self.args:
            # Use the standard Python argument definitions from the args object
            _, call_args, _, arg_types = self.args.python_argdefs()
        else:
            # Create Python tensor arguments for CuteDSL function call
            call_args = []
            arg_types = []
            
            # Process input nodes - these are PyTorch tensors passed directly
            for i, input_node in enumerate(self.input_nodes):
                if hasattr(input_node, 'get_name'):
                    buffer_name = input_node.get_name()
                    call_args.append(buffer_name)  # Direct tensor, not c_void_p
                else:
                    call_args.append(f"arg{i}")
                arg_types.append("torch.Tensor")
            
            # Process output node - also a PyTorch tensor
            if self.output_node:
                if hasattr(self.output_node, 'get_name'):
                    buffer_name = self.output_node.get_name()
                    call_args.append(buffer_name)  # Direct tensor, not c_void_p
                else:
                    call_args.append("out")
                arg_types.append("torch.Tensor")
        
        # Generate the Python function call for CuteDSL
        wrapper.generate_kernel_call(
            name,
            call_args, 
            triton=False,  # CuteDSL is not Triton
            arg_types=arg_types
        )




