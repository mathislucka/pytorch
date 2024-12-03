# mypy: allow-untyped-defs
# This file establishes the public comptime interface to Dynamo.
# This allows Dynamo users to execute arbitrary Python code while
# Dynamo is symbolically evaluating their original programs.
#
# The goal of the public API is to give users rope, without actually
# leaking private implementation details of Dynamo.

import builtins
import dis
import time
import traceback
from typing import Optional, Union

import torch
from torch.fx.experimental.symbolic_shapes import free_symbols

from .exc import unimplemented
from .variables import CellVariable
from .variables.constant import ConstantVariable
from .variables.tensor import SymNodeVariable


class ComptimeVar:
    """
    A ComptimeVar represents a Python value, at some particular point
    in time, in the Python code we are symbolically evaluating with
    torchdynamo.  This must be distinguished from a runtime value, as
    at compile-time there are some properties of the variable we
    do not know (for example, if the ComptimeVar represents a Tensor,
    we only know metadata about the tensor; we do NOT know what the
    actual data in the Tensor is.)
    """

    def __init__(self, v) -> None:
        self.__variable = v

    def as_proxy(self):
        """
        Returns an fx.Proxy (or tuple/list of fx.Proxy) representing
        this variable in the FX graph we are assembling to pass
        to the user compiler.

        This method only works for variables we actually track in
        the FX graph, aka Tensors (and ints, if you are compiling
        with dynamic shapes).  In particular, if you have a list
        or tuple of tensors, you will get a list/tuple of proxies
        (not a single proxy representing the entire list/tuple).
        """
        return self.__variable.as_proxy()

    def is_proxy(self):
        """
        Returns True if as_proxy() would succeed.
        """
        return self.__variable.is_proxy()

    def as_fake(self):
        """
        Returns a "fake" value (either a FakeTensor or a SymInt)
        representing the variable in question.  This only works
        for variables that denote Tensor or int.  You can use
        this to query metadata; e.g., v.as_fake().size(0) will
        tell you the compile-time known size of the tensor.

        WARNING: Do NOT mutate the returned tensor.
        """
        return self.__variable.as_proxy().node.meta["example_value"]

    def size(self, dim: Optional[int] = None) -> Union[int, torch.SymInt]:
        """
        Returns the size of the tensor (if dim is None) or the size
        at the dimension dim.  The returned size may be a SymInt.
        """
        return self.as_fake().size(dim)

    def python_type(self):
        """
        Returns what type(v) would have returned for the variable
        at compile time.
        """
        return self.__variable.python_type()

    def as_python_constant(self):
        """
        Returns the Python value this variable would have, but only if it is
        completely known at compile-time (e.g., it is constant).

        WARNING: Do NOT mutate the returned constant.  The returned constant
        may or may not correspond to the actual value this variable may take
        on at runtime; for example, if the variable in question is a constant
        list, we may return a copy of that list.
        """
        return self.__variable.as_python_constant()

    def is_python_constant(self):
        """
        Returns True if as_python_constant would succeed.
        """
        return self.__variable.is_python_constant()

    def is_dynamic(self):
        if isinstance(self.__variable, SymNodeVariable):
            fs = free_symbols(self.__variable.sym_num)
            return bool(fs)
        return False

    def force_static(self):
        """
        Forces that a value is static, inducing a guard on its specific value
        """
        if isinstance(self.__variable, SymNodeVariable):
            self.__variable.evaluate_expr()
        elif isinstance(self.__variable, ConstantVariable):
            # TODO: Maybe complain if this isn't a int/bool/float variable
            pass
        else:
            raise AssertionError(
                f"cannot force {self.__variable} ({type(self.__variable)}) static"
            )

    def _i_will_not_complain_if_bc_breaks_VariableTracker(self):
        """
        Returns the internal data structure VariableTracker that Dynamo uses
        to represent variables at compile time.  There are no BC guarantees on
        this API and WE RESERVE THE RIGHT TO BREAK YOUR CODE if you rely on
        it.
        """
        return self.__variable

    def __repr__(self) -> str:
        return self.__variable.debug_repr()

    # TODO: API for adding a custom guard


class ComptimeContext:
    """
    This context class provides access to a public API for Dynamo's internals.
    If there is something here you would find useful that is missing, please
    file a feature request at https://github.com/pytorch/pytorch/
    """

    def __init__(self, tx) -> None:
        self.__tx = tx

    def get_local(self, name: str, *, stacklevel=0) -> ComptimeVar:
        """
        Retrieve the compile-time known information about a local.
        """
        tx = self.__get_tx(stacklevel)
        var = tx.symbolic_locals[name]

        # Auto-dereference when accessing cell locals in python.
        if isinstance(var, CellVariable):
            return ComptimeVar(tx.output.side_effects.load_cell(var))

        return ComptimeVar(var)

    def graph_break(self, msg="ComptimeContext.graph_break"):
        """
        Manually trigger a graph break
        """
        unimplemented(msg)

    def graph(self):
        """
        Retrieve the partially constructed FX graph that would be
        passed to the user compiler after compilation.
        """
        return self.__tx.output.graph

    def assert_static(self, val):
        """
        Asserts that the int is static (and not dynamic, per dynamic shapes)
        """
        assert (
            not val.is_dynamic()
        ), "expected static but got dynamic (run with TORCH_LOGS=dynamic for more info)"

    def print_graph(self, *, verbose=True, file=None):
        """
        Print the partially constructed FX graph that would be passed
        to the user compiler after compilation.
        """
        print(
            self.__tx.output.graph.python_code("self", verbose=verbose).src, file=file
        )

    def parent(self):
        return ComptimeContext(self.__tx.parent)

    def __get_tx(self, stacklevel):
        tx = self.__tx
        for _ in range(stacklevel):
            tx = tx.parent
        return tx

    def print(self, val, *, file=None):
        print(repr(val), file=file)

    def print_disas(self, *, file=None, stacklevel=0):
        """
        Print the current series of opcodes being executed (not including
        parent frames), including where you are in the particular opcode
        stream.
        """
        tx = self.__get_tx(stacklevel)
        print(
            dis.Bytecode(
                tx.f_code,
                current_offset=tx.instructions[tx.instruction_pointer].offset,
            ).dis(),
            file=file,
        )

    def print_value_stack(self, *, file=None, stacklevel=0):
        """
        Print the current Python value stack.  Note that this is NOT the same
        as the traceback; use print_bt() to print that.  Note that at
        stacklevel=0, this will typically be empty, as comptime cannot
        currently be used in an expression context where there would be
        intermediates on the stack.  If you would find this useful, please
        file a bug at https://github.com/pytorch/pytorch/

        NB: Stack grows downwards in our print
        """
        tx = self.__get_tx(stacklevel)
        for s in tx.stack:
            print(f"- {s.debug_repr()}", file=file)

    def print_locals(self, *, file=None, stacklevel=0):
        """
        Print all of the locals available in the current context.
        By default this view is very limited; you can get more information
        about any individual local using get_local().
        """
        tx = self.__get_tx(stacklevel)
        for k, v in tx.symbolic_locals.items():
            print(f"{k} = {v.debug_repr()}", file=file)

    def print_bt(self, *, file=None, stacklevel=0):
        """
        Print the user code backtrace, starting at the beginning of the
        frame Dynamo started evaluating.  Note that this MAY NOT go all
        the way to the torch.compile invocation, as we may have done
        a graph break and are compiling an intermediate frame as the
        starting point.  If you think the other behavior would be better,
        file a bug at https://github.com/pytorch/pytorch/
        """
        stack = []
        tx = self.__get_tx(stacklevel)
        while tx is not None:
            stack.append(tx.frame_summary())
            tx = getattr(tx, "parent", None)
        print(
            "".join(traceback.StackSummary.from_list(reversed(stack)).format()),
            file=file,
        )

    def print_guards(self, *, file=None):
        """
        Print the currently installed guards for the Dynamo context.
        This does NOT include guards associated with variables that
        may or may not be installed in the future if those variables
        are used.
        """
        # TODO: improve print format, current guard format is extremely
        # verbose
        print(
            "\n".join(f"{repr(guard)}" for guard in sorted(self.__tx.output.guards)),
            file=file,
        )

    def _i_will_not_complain_if_bc_breaks_InstructionTranslator(self):
        """
        Returns the internal data structure InstructionTranslator that Dynamo
        uses to track state of symbolic evaluation.  There are no BC
        guarantees on this API and WE RESERVE THE RIGHT TO BREAK YOUR CODE if
        you rely on it.
        """
        return self.__tx

    def sleep(self, sec):
        time.sleep(sec)


class _Comptime:
    @staticmethod
    def __call__(fn, fallback_fn=lambda: None):
        """fn gets called at compile time in TorchDynamo, calls fallback_fn otherwise"""
        fallback_fn()

    # Convenience wrappers that are more compact to use

    @staticmethod
    def graph_break():
        comptime(lambda ctx: ctx.graph_break())

    @staticmethod
    def print(e):
        comptime(lambda ctx: ctx.print(ctx.get_local("e")), lambda: print(e))

    @staticmethod
    def print_graph():
        comptime(lambda ctx: ctx.print_graph())

    @staticmethod
    def print_disas(*, stacklevel=0):
        comptime(
            lambda ctx: ctx.print_disas(
                stacklevel=ctx.get_local("stacklevel").as_python_constant() + 1
            )
        )

    @staticmethod
    def print_value_stack(*, stacklevel=0):
        comptime(
            lambda ctx: ctx.print_value_stack(
                stacklevel=ctx.get_local("stacklevel").as_python_constant() + 1
            )
        )

    # This is a more useful variant of print_value_stack that can be used
    # in an expression context; e.g., x + print_value_stack_and_return(y + z),
    # you will see x on the stack prior to the addition operation
    @staticmethod
    def print_value_stack_and_return(e, *, stacklevel=0):
        comptime(
            lambda ctx: ctx.print_value_stack(
                stacklevel=ctx.get_local("stacklevel").as_python_constant() + 1
            )
        )
        return e

    @staticmethod
    def print_locals(*, stacklevel=0):
        comptime(
            lambda ctx: ctx.print_locals(
                stacklevel=ctx.get_local("stacklevel").as_python_constant() + 1
            )
        )

    @staticmethod
    def print_bt(*, stacklevel=0):
        comptime(
            lambda ctx: ctx.print_bt(
                stacklevel=ctx.get_local("stacklevel").as_python_constant() + 1
            )
        )

    @staticmethod
    def print_guards():
        comptime(lambda ctx: ctx.print_guards())

    @staticmethod
    def assert_static(val):
        comptime(lambda ctx: ctx.assert_static(ctx.get_local("val")))

    @staticmethod
    def force_static(val):
        comptime(lambda ctx: ctx.get_local("val").force_static())

    @staticmethod
    def breakpoint():
        """
        Like pdb breakpoint(), but drop into pdb whenever this line
        of code is compiled by dynamo.  Use it by putting
        this in your model code::

            from torch._dynamo.comptime import comptime
            comptime.breakpoint()

        And then, inside pdb, you can access 'ctx' to query things
        about the compilation context::

            (Pdb) !ctx.print_bt()
            (Pdb) !ctx.print_locals()
            (Pdb) p ctx.get_local("attention").as_fake()
        """

        def inner(inner_ctx):
            ctx = inner_ctx.parent()
            builtins.breakpoint()

        comptime(inner)

    @staticmethod
    def sleep(sec):
        comptime(lambda ctx: ctx.sleep(ctx.get_local("sec").as_python_constant()))


comptime = _Comptime()
