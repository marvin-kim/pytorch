# mypy: allow-untyped-defs
import itertools
import logging
import operator
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

import torch
from torch._higher_order_ops.triton_kernel_wrap import (
    kernel_side_table,
    triton_kernel_wrapper_functional,
)
from torch._inductor import inductor_prims
from torch._inductor.fx_utils import get_node_storage, is_node_realized
from torch._inductor.lowering import (
    inplaceable_foreach_ops as inplaceable_foreach_ops_lowerings,
)
from torch._inductor.virtualized import V
from torch.fx.immutable_collections import immutable_dict
from torch.fx.passes.reinplace import _is_view_op
from torch.utils import _pytree as pytree


log = logging.getLogger(__name__)
aten = torch.ops.aten


@dataclass(frozen=True)
class InplaceableOp:
    inplace_op: Callable[..., Any]
    mutated_arg: int
    extra_check: Callable[[torch.fx.Node], bool] = lambda node: True


_SCATTER_OP_TO_VIEW = {
    torch.ops.aten.diagonal_scatter.default: torch.ops.aten.diagonal.default,
    torch.ops.aten.select_scatter.default: torch.ops.aten.select.int,
    torch.ops.aten.slice_scatter.default: torch.ops.aten.slice.Tensor,
    torch.ops.aten.as_strided_scatter.default: torch.ops.aten.as_strided.default,
}
_VIEW_OP_TO_SCATTER = {v: k for k, v in _SCATTER_OP_TO_VIEW.items()}


def graph_call_function(graph: torch.fx.Graph, fn, *args, **kwargs):
    fake_args, fake_kwargs = pytree.tree_map(
        lambda node: node.meta["val"] if isinstance(node, torch.fx.Node) else node,
        (args, kwargs),
    )
    with V.fake_mode:
        fake_result = fn(*fake_args, **fake_kwargs)

    node = graph.call_function(fn, args, kwargs)
    node.meta["val"] = fake_result
    return node


@dataclass
class ViewOp:
    target: torch._ops.OpOverload
    args: Tuple[Any, ...]
    kwargs: Dict[str, Any]


def _inplace_generalized_scatter(
    inp: torch.Tensor, src: torch.Tensor, view_ops: List[ViewOp]
) -> torch.Tensor:
    tmp = inp
    for view in view_ops:
        fake_args, fake_kwargs = pytree.tree_map(
            lambda node: node.meta["val"] if isinstance(node, torch.fx.Node) else node,
            (view.args, view.kwargs),
        )
        tmp = view.target(tmp, *fake_args, **fake_kwargs)
    try:
        tmp.copy_(src)
    except RuntimeError as e:
        raise RuntimeError(
            f"shape error in scatter op, can not broadcast {src.shape} to {tmp.shape}"
        ) from e
    return inp


def _generalized_scatter(
    inp: torch.Tensor, src: torch.Tensor, view_ops: List[ViewOp]
) -> torch.Tensor:
    out = inp.clone()
    return _inplace_generalized_scatter(out, src, view_ops)


def _decompose_scatter_functional_helper(
    graph: torch.fx.Graph,
    inp: torch.Tensor,
    src: torch.Tensor,
    view_ops: List[ViewOp],
) -> torch.fx.Node:
    view_op, view_ops_tail = view_ops[0], view_ops[1:]

    if view_ops_tail:
        view = graph_call_function(
            graph, view_op.target, inp, *view_op.args, **view_op.kwargs
        )
        src = _decompose_scatter_functional_helper(graph, view, src, view_ops[1:])  # type: ignore[assignment]

    return graph_call_function(
        graph,
        _VIEW_OP_TO_SCATTER[view_op.target],
        inp,
        src,
        *view_op.args,
        **view_op.kwargs,
    )


def _decompose_scatter_functional(
    graph: torch.fx.Graph, node: torch.fx.Node
) -> torch.fx.Node:
    """Decompose _generalized_scatter to a sequence of view_scatter operations

    e.g. _generalized_scatter(inp, src, [(aten.slice, 0, 0, 10), (aten.slice, 1, 10, -10)])

    will become

    view = aten.slice(inp, 0, 0, 10)
    view_updated = aten.slice_scatter(view, src, 1, 10, -10)
    inp_updated = aten.slice_scatter(inp, view_updated, 0, 0, 10)
    """
    assert node.target is _generalized_scatter
    inp, src, view_ops = node.args
    return _decompose_scatter_functional_helper(graph, *node.args)  # type: ignore[arg-type]


def _decompose_scatter_mutating(
    graph: torch.fx.Graph, node: torch.fx.Node
) -> torch.fx.Node:
    """Decompose _generalized_scatter using mutations

    e.g. _generalized_scatter(inp, src, [(aten.slice, 0, 0, 10), (aten.slice, 1, 10, -10)])

    will become

    inp_updated = aten.clone(inp)
    slice1 = aten.slice(inp_updated, 0, 0, 10)
    slice2 = aten.slice(slice1, 1, 10, -10)
    slice2.copy_(src)

    """
    assert node.target in (_generalized_scatter, _inplace_generalized_scatter)
    inp, src, view_ops = node.args
    assert not node.kwargs

    if node.target is _generalized_scatter:
        inp = graph_call_function(graph, aten.clone, inp)

    tmp = inp
    for view in view_ops:  # type: ignore[union-attr]
        tmp = graph_call_function(graph, view.target, tmp, *view.args, **view.kwargs)  # type: ignore[union-attr]

    graph_call_function(graph, aten.copy_.default, tmp, src)
    return inp  # type: ignore[return-value]


# View ops whose view_scatter op is lowered into mutations anyway,
# so is never a pessimisation to decompose.
_ALWAYS_MUTATING_SCATTER_OPS = {
    aten.as_strided.default,
    aten.diagonal.default,
}


def scatter_always_uses_mutation(node: torch.fx.Node) -> bool:
    _, _, view_ops = node.args
    return any(view.target in _ALWAYS_MUTATING_SCATTER_OPS for view in view_ops)  # type: ignore[union-attr]


def should_reinplace_scatter(node: torch.fx.Node) -> bool:
    """Choose between mutating and functional scatter decompositions

    Reinplacing view scatter ops can be pessimising as it blocks fusion with the
    input or output tensor computations. However, it is still profitable if the
    input and output would have been realized anyway.

    """
    inp, src, view_ops = node.args

    # Mutating scatter ops unconditionally realize input and output
    if scatter_always_uses_mutation(node):
        return True

    if is_node_realized(inp) and is_node_realized(node):  # type: ignore[arg-type]
        return True

    # If the output is copied back into the input, this forces both to be
    # realized as the output is a user of the input
    if inp.op in ("placeholder", "get_attr") and any(  # type: ignore[union-attr]
        user.target is aten.copy_.default and user.args[0] is inp for user in node.users
    ):
        return True

    # Otherwise, assume fusions will make functional variants profitable
    return False


def decompose_generalized_scatter(graph: torch.fx.Graph) -> None:
    """Replace _generalized_scatter with normal aten ops"""
    for node in itertools.chain(
        graph.find_nodes(op="call_function", target=_generalized_scatter),
        graph.find_nodes(op="call_function", target=_inplace_generalized_scatter),
    ):
        use_mutation = (
            node.target is _inplace_generalized_scatter
            or scatter_always_uses_mutation(node)
        )

        with graph.inserting_before(node):
            if use_mutation:
                new_node = _decompose_scatter_mutating(graph, node)
            else:
                new_node = _decompose_scatter_functional(graph, node)

        node.replace_all_uses_with(new_node)
        graph.erase_node(node)


def canonicalize_view_scatter_ops(graph: torch.fx.Graph) -> None:
    """
    This canonicalizes view scatter ops into a generalized form, defined as:
      def scatter(inp, src, views):
        tmp = inp.clone()
        for view in views:
          tmp = view(tmp)
        tmp.copy_(src)

    We also fuse consecutive view scatter ops of the form
        a = scatter(view2(self), src, [view1])
        b = scatter(self, a, [view2])
    which can be rewritten as
        b = scatter(self, src, [view2, view1])
        a = view2(b)

    This is both more efficient as we only do a single scatter, and also
    easier to reinplace since there is only one use of `self`
    """

    node_to_view_base: Dict[torch.fx.Node, torch.fx.Node] = {}
    node_to_view_op: Dict[torch.fx.Node, List[ViewOp]] = defaultdict(list)

    def handle_views(node: torch.fx.Node):
        inp = node.args[0]
        node_to_view_base[node] = node_to_view_base.get(inp, inp)  # type: ignore[arg-type]
        node_to_view_op[node] = [
            *node_to_view_op[inp],  # type: ignore[index]
            ViewOp(
                node.target,  # type: ignore[arg-type]
                args=node.args[1:],
                kwargs=node.kwargs,
            ),
        ]

    def handle_view_scatter(node: torch.fx.Node):
        assert len(node.args) >= 2
        inp, src = node.args[:2]

        scatter_view_op = ViewOp(
            _SCATTER_OP_TO_VIEW[node.target],
            args=node.args[2:],
            kwargs=node.kwargs,
        )

        def can_fuse():
            if src.target is not _generalized_scatter:  # type: ignore[union-attr]
                return False
            src_inp, src_src, src_scatter_view_op = src.args  # type: ignore[union-attr]

            inp_base = node_to_view_base.get(inp, inp)  # type: ignore[arg-type]
            src_base = node_to_view_base.get(src_inp, src_inp)  # type: ignore[arg-type]
            return inp_base is src_base and node_to_view_op[src_inp] == [  # type: ignore[index]
                *node_to_view_op[inp],  # type: ignore[index]
                scatter_view_op,
            ]

        if not can_fuse():
            with graph.inserting_before(node):
                new_node = graph_call_function(
                    graph,
                    _generalized_scatter,
                    inp,
                    src,
                    [scatter_view_op],
                )
            node.replace_all_uses_with(new_node)
            graph.erase_node(node)
            return

        src_inp, src_src, src_scatter_view_op = src.args  # type: ignore[union-attr]
        with graph.inserting_before(src):
            new_node = graph_call_function(
                graph,
                _generalized_scatter,
                inp,
                src_src,
                [scatter_view_op, *src_scatter_view_op],  # type: ignore[misc]
            )
            node.replace_all_uses_with(new_node)
            graph.erase_node(node)

            if src.users:  # type: ignore[union-attr]
                new_src = graph_call_function(
                    graph,
                    _SCATTER_OP_TO_VIEW[node.target],
                    new_node,
                    *node.args[2:],
                    **node.kwargs,
                )

                handle_views(new_src)
                src.replace_all_uses_with(new_src)  # type: ignore[union-attr]

            graph.erase_node(src)

    for node in graph.nodes:
        if _is_view_op(node.target):
            handle_views(node)
        elif node.target in _SCATTER_OP_TO_VIEW:
            handle_view_scatter(node)


inplaceable_ops = {
    aten.index_put.default: InplaceableOp(aten.index_put_.default, 0),
    aten._unsafe_index_put.default: InplaceableOp(inductor_prims._unsafe_index_put_, 0),
    _generalized_scatter: InplaceableOp(
        _inplace_generalized_scatter,
        0,
        extra_check=should_reinplace_scatter,
    ),
}

try:
    c10d_functional = torch.ops._c10d_functional
    inplaceable_collective_ops = {
        c10d_functional.all_reduce.default: InplaceableOp(
            c10d_functional.all_reduce_.default, 0
        ),
        c10d_functional.all_reduce_coalesced.default: InplaceableOp(
            c10d_functional.all_reduce_coalesced_.default, 0
        ),
    }
    inplaceable_ops.update(inplaceable_collective_ops)
except AttributeError:
    # _c10d_functional ops are only available when torch
    # is built with USE_DISTRIBUTED=1.
    pass

inplaceable_foreach_ops: Dict[torch._ops.OpOverload, InplaceableOp] = {}
for outplace_op, inplace_op in inplaceable_foreach_ops_lowerings.items():
    inplaceable_foreach_ops[outplace_op] = InplaceableOp(inplace_op, 0)


inplaceable_triton_ops = {triton_kernel_wrapper_functional}


# Operators that don't depend on the tensor data
META_ONLY_OPS = {
    aten.sym_size.int,
    aten.sym_stride.int,
    aten.sym_numel.default,
    aten.sym_storage_offset.default,
}


def reinplace_inplaceable_ops_core(graph: torch.fx.Graph) -> None:
    """
    Reinplaces in-placeable operations.
    If there are no uses of a view of the mutated arg after the current node,
    it is possible to inplace the op.
    This above algorithm could be justified by observing side effects. While
    we traverse the graph in forwards direction, only latter nodes could view
    side effects of the current node. If the current node is not used later as
    well as no view of this node is used later in the graph, then it is safe to
    inplace as there would be no way to observe the side effects.
    This condition is slightly different for graph inputs where they can only
    be inplaced if the above condition is true and there's a copy_ in the
    epilogue that signals that the caller wants to observe the mutation.

    Unlike JIT Inductor, AOTInductor currently unlifts weights and buffers from
    input args, so instead of checking mutation on placeholder, AOTInductor
    checks mutation on get_attr. This is subject to change in future.
    """

    copy_args_to_copy_nodes = {}
    # maps argument to the first copy_ node that mutates it.
    copy_nodes = {}
    mutated_inputs = set()
    storage_to_nodes = defaultdict(list)
    node_order: Dict[Any, int] = {}
    for i, node in enumerate(reversed(graph.nodes)):
        node_order[node] = len(graph.nodes) - i - 1
        storage_to_nodes[get_node_storage(node)].append(node)
        if node.target == aten.copy_.default and node.args[0].op in (
            "placeholder",
            "get_attr",
        ):
            dst = node.args[0]
            src = node.args[1]
            # If the target is a getitem and it indexes a possible clone,
            # then skip over it
            if src.target == operator.getitem and (
                (
                    src.args[0].target == triton_kernel_wrapper_functional
                    and src.args[0].kwargs["kwargs"][src.args[1]] == node.args[0]
                )
                or (src.args[0].target in inplaceable_foreach_ops)
                or (src.args[0].target == torch.ops.higher_order.auto_functionalized)
            ):
                src = src.args[0]

            copy_args_to_copy_nodes[(dst, src)] = node
            copy_nodes[dst] = node

            mutated_inputs.add(node.args[0])

    def any_use_of_views_after_node(node, shared_view_nodes, *, copy_node, mutated_arg):
        node_loc = node_order[node]
        copy_node_loc = node_order[copy_node] if copy_node is not None else None

        def is_meta_only_user(node):
            if _is_view_op(node.target):
                return all(is_meta_only_user(u) for u in node.users)
            return node.target in META_ONLY_OPS

        for view in shared_view_nodes:
            for user in view.users:
                user_loc = node_order[user]
                # Skip all users before node
                if user_loc <= node_loc:
                    continue
                # Ignore uses after the copy_ epilogue node, where the input
                # has already been mutated anyway
                if copy_node_loc is not None and copy_node_loc <= user_loc:
                    continue
                # Reinplacing does not change shape metadata
                if is_meta_only_user(user):
                    continue
                # If our graph looks like:
                # foo(mutated_arg)
                # mutated_arg.copy_(other)
                # then it's safe for us to reinplace foo because mutated_arg
                # will get overwritten anyways.
                if (
                    user.target is torch.ops.aten.copy_.default
                    and mutated_arg is user.args[0]
                ):
                    continue
                return True
        return False

    def can_inplace(node, mutated_arg):
        if isinstance(mutated_arg, (list, tuple)):
            unique_storages = {get_node_storage(arg) for arg in mutated_arg}
            if len(unique_storages) != len(mutated_arg):
                # at least two Tensors in mutated_arg alias each other, so we can't reinplace it.
                # We can probably do better (that is, reinplace one of them and clone the other)
                # but that requires more work and mutable List[Tensor] are not that common.
                return False
            return all(can_inplace(node, arg) for arg in mutated_arg)

        if get_node_storage(mutated_arg) is None:
            return False
        shared_view_nodes = storage_to_nodes[get_node_storage(mutated_arg)]

        viewed_input = None
        if mutated_arg.op in ("placeholder", "get_attr"):
            viewed_input = mutated_arg
        else:
            for view in shared_view_nodes:
                if view.op in ("placeholder", "get_attr"):
                    viewed_input = view
                    break

        if viewed_input:
            # Get the first copy_ node that mutates the mutated_arg.
            copy_node = copy_nodes.get(viewed_input, None)
            if copy_node is None:
                # There is no copy_ back to the candidate mutated_arg (which is a graph input).
                # Therefore the semantics of the program are that it does not mutate
                # mutated_arg, so we cannot re-inplace it.
                return False
            if any_use_of_views_after_node(
                node, shared_view_nodes, copy_node=copy_node, mutated_arg=mutated_arg
            ):
                return False

            return True
        else:
            return not any_use_of_views_after_node(
                node, shared_view_nodes, copy_node=None, mutated_arg=mutated_arg
            )

    def log_inplace_results(
        node_name,
        old_tensors_to_clone,
        tensors_to_clone,
        possibly_missed_reinplacing_opportunities,
    ):
        log.info(
            "For node %s, attempted to reinplace %s. We were unable to reinplace %s; "
            "%s (if non-empty) are possible missed reinplacing opportunities that may be bad for "
            "memory usage and performance.",
            node_name,
            old_tensors_to_clone,
            tensors_to_clone,
            possibly_missed_reinplacing_opportunities,
        )
        torch._dynamo.utils.counters["inductor"][
            "possibly_missed_reinplacing_opportunities"
        ] += len(possibly_missed_reinplacing_opportunities)

    replace_dict: Dict[torch.fx.Node, torch.fx.Node] = {}

    def reinplace_and_refine_tensors_to_clone(old_tensors_to_clone, kwargs, node_name):
        tensors_to_clone: List[str] = []
        storage_of_reinplaced_args = set()
        possibly_missed_reinplacing_opportunities = []

        def tensor_with_same_storage_already_reinplaced(arg):
            if isinstance(arg, (list, tuple)):
                return any(
                    get_node_storage(a) in storage_of_reinplaced_args for a in arg
                )
            return get_node_storage(mutated_arg) in storage_of_reinplaced_args

        for arg in old_tensors_to_clone:
            assert arg in kwargs
            mutated_arg = kwargs[arg]

            # Let's say we have:
            # - op(x, y) that mutates both x and y
            # - new_x, new_y = functional_op(x, y) is the functional variant
            # If we are presented with functional_op(x, x), we must not reinplace
            # this into op(x, x), because then it would be writing to the same Tensor.
            # Instead, it's OK to reinplace one of them and to clone the other:
            # >>> y = x.clone()
            # >>> op(x, y)
            # This also applies if we have views: functional_op(x, x[0])
            # should not reinplace into op(x, x[0]).
            should_attempt_reinplace = not tensor_with_same_storage_already_reinplaced(
                mutated_arg
            )
            if should_attempt_reinplace and can_inplace(node, mutated_arg):
                copy_node = copy_args_to_copy_nodes.get((mutated_arg, node))
                if copy_node is not None:
                    replace_dict[copy_node] = copy_node.args[0]
                for user in node.users:
                    if user.target == operator.getitem and user.args[1] == arg:
                        replace_dict[user] = mutated_arg

                if isinstance(mutated_arg, (list, tuple)):
                    for a in mutated_arg:
                        storage_of_reinplaced_args.add(get_node_storage(a))
                else:
                    storage_of_reinplaced_args.add(get_node_storage(mutated_arg))
            else:
                if should_attempt_reinplace:
                    possibly_missed_reinplacing_opportunities.append(arg)
                tensors_to_clone.append(arg)

        log_inplace_results(
            node_name,
            old_tensors_to_clone,
            tensors_to_clone,
            possibly_missed_reinplacing_opportunities,
        )
        return tensors_to_clone

    for node in graph.nodes:
        if (inplaceable_op := inplaceable_ops.get(node.target, None)) is not None:
            mutated_arg = node.args[inplaceable_op.mutated_arg]
            if can_inplace(node, mutated_arg) and inplaceable_op.extra_check(node):
                # TODO(yifu): this doesn't properly remove copy epilogues for
                # ops that mutate multiple inputs. Need to revise the copy
                # node tracking logic to support the case.
                copy_node = copy_args_to_copy_nodes.get((mutated_arg, node))
                if copy_node is not None:
                    replace_dict[copy_node] = copy_node.args[0]
                node.target = inplaceable_op.inplace_op
        elif node.target == torch.ops.higher_order.auto_functionalized:
            from torch._higher_order_ops.auto_functionalize import (
                deserialize_views_meta,
                get_mutable_args,
            )

            _mutable_op = node.args[0]
            tensors_to_clone, arg_types = get_mutable_args(_mutable_op)

            kwargs = node.kwargs
            all_bases = kwargs["_all_bases"]
            args_view_info = deserialize_views_meta(
                tensors_to_clone, arg_types, kwargs, all_bases, pop_args=False
            )

            # Don't try to reinplace Optional[Tensor] args that are None.
            tensors_to_clone = [
                t for t in tensors_to_clone if args_view_info[t] is not None
            ]

            old_tensors_to_clone = tensors_to_clone
            possibly_missed_reinplacing_opportunities = []

            # for each base in all_bases check if its in-placable.
            inplaceable_bases = [can_inplace(node, arg) for arg in all_bases]

            # In general if a base satisfy inplace requirements then all views on top it are also inplacable.

            # Note1: One exception is when two bases in all_bases shares storage, in that case this means thats some pass
            # have changed auto_functionalize, like CSE and added such aliasing, because when auto_functionalize is created, it
            # assert that all_bases have unique storages.

            # To avoid mutating same storage by the custom op that was not originally mutated in the original program,
            # we only allow inplacing one arg in such condition. `storage_to_inplace_once`` tracks identify tensors such that only
            # one tensors of that storage should be inplaced and `inplaced_storage` tracks inplaced storages.

            storage_to_inplace_once = set()
            seen_storage = set()
            for i, base in enumerate(all_bases):
                storage = get_node_storage(base)
                if storage in seen_storage:
                    storage_to_inplace_once.add(storage)
                else:
                    seen_storage.add(storage)

            seen_storage = None

            # only include inplaced_storage for storages in storage_to_inplace_once
            inplaced_storage = set()

            do_not_clone = []
            for arg_name in tensors_to_clone:
                view_info = args_view_info[arg_name]

                if view_info is None:
                    continue

                if isinstance(view_info, (list, tuple)):
                    view_info_no_none = [v for v in view_info if v is not None]

                    unique_storages = {
                        get_node_storage(v.base) for v in view_info_no_none
                    }

                    # see Note1
                    if any(
                        (
                            storage in storage_to_inplace_once
                            and storage in inplaced_storage
                        )
                        for storage in unique_storages
                    ):
                        # we do not consider this in possibly_missed_reinplacing_opportunities
                        continue

                    # if any two items in the list shares storage we do not inplace.
                    # TODO revisit this, why not?
                    if len(unique_storages) != len(view_info_no_none):
                        possibly_missed_reinplacing_opportunities.append(arg_name)
                        continue

                    # if any of the items in the list has non inplaceable base we do not inplace.
                    if any(
                        not inplaceable_bases[item_view.base_index]
                        for item_view in view_info_no_none
                    ):
                        possibly_missed_reinplacing_opportunities.append(arg_name)
                        continue

                    for item_view in view_info_no_none:
                        storage = get_node_storage(item_view.base)
                        if storage in storage_to_inplace_once:
                            inplaced_storage.add(storage)

                    # we inplace this arg yeey!
                    do_not_clone.append(arg_name)

                else:
                    # arg is a single tensor
                    storage = get_node_storage(view_info.base)

                    # see Note1
                    if (
                        storage in storage_to_inplace_once
                        and storage in inplaced_storage
                    ):
                        # we do not consider this in possibly_missed_reinplacing_opportunities
                        continue

                    if not inplaceable_bases[view_info.base_index]:
                        possibly_missed_reinplacing_opportunities.append(arg_name)
                        continue

                    if storage in storage_to_inplace_once:
                        inplaced_storage.add(storage)

                    # we inplace this arg yeey!
                    do_not_clone.append(arg_name)

            for t in do_not_clone:
                tensors_to_clone.remove(t)

            log_inplace_results(
                _mutable_op._name,
                old_tensors_to_clone,
                tensors_to_clone,
                possibly_missed_reinplacing_opportunities,
            )

            # Stash the metadata. There is a pass later on where we decompose
            # auto_functionalized into clones + a mutable op; this metadata
            # tells the decomp to only clone the following inputs
            node.meta["only_clone_these_tensors"] = tensors_to_clone
        elif node.target in inplaceable_triton_ops:
            kernel_idx = node.kwargs["kernel_idx"]
            kernel = kernel_side_table.get_kernel(kernel_idx)
            from triton.runtime.autotuner import Autotuner
            from triton.runtime.jit import JITFunction

            if isinstance(kernel, JITFunction):
                kernel_name = kernel.fn.__name__
            elif isinstance(kernel, Autotuner):
                kernel_name = kernel.base_fn.__name__
            else:
                raise AssertionError("Unknown triton kernel type")

            # inplaceable_triton_ops take an additional argument called
            # tensors_to_clone which contain a list of tensors to clone
            # This pass iterates over them and sees which ones are safe
            # to eliminate (i.e. no longer need the clones)
            tensors_to_clone = reinplace_and_refine_tensors_to_clone(
                node.kwargs["tensors_to_clone"], node.kwargs["kwargs"], kernel_name
            )

            kwargs = dict(node.kwargs)
            kwargs["tensors_to_clone"] = tensors_to_clone
            node.kwargs = immutable_dict(kwargs)
        elif (
            inplaceable_op := inplaceable_foreach_ops.get(node.target, None)
        ) is not None:
            mutated_args = node.args[inplaceable_op.mutated_arg]

            if not all((arg, node) in copy_args_to_copy_nodes for arg in mutated_args):
                continue

            if can_inplace(node, mutated_args):
                for arg in mutated_args:
                    copy_node = copy_args_to_copy_nodes[(arg, node)]
                    replace_dict[copy_node] = copy_node.args[0]

                node.target = inplaceable_op.inplace_op
    for node, replacement in replace_dict.items():
        while replacement in replace_dict:
            replacement = replace_dict[replacement]
        replace_dict[node] = replacement

        node.replace_all_uses_with(replacement)
        graph.erase_node(node)


def reinplace_inplaceable_ops(graph: torch.fx.Graph) -> None:
    canonicalize_view_scatter_ops(graph)
    reinplace_inplaceable_ops_core(graph)
    decompose_generalized_scatter(graph)
