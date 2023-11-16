# Copyright © 2023 Pathway

from __future__ import annotations

from collections.abc import Callable, Iterable

from pathway.internals import api, column, environ, parse_graph as graph, table, trace
from pathway.internals.column_path import ColumnPath
from pathway.internals.graph_runner.async_utils import new_event_loop
from pathway.internals.graph_runner.row_transformer_operator_handler import (  # noqa: registers handler for RowTransformerOperator
    RowTransformerOperatorHandler,
)
from pathway.internals.graph_runner.scope_context import ScopeContext
from pathway.internals.graph_runner.state import ScopeState
from pathway.internals.graph_runner.storage_graph import OperatorStorageGraph
from pathway.internals.helpers import StableSet
from pathway.internals.monitoring import MonitoringLevel, monitor_stats
from pathway.internals.operator import ContextualizedIntermediateOperator, Operator
from pathway.persistence import Config as PersistenceConfig


class GraphRunner:
    """Runs evaluation of ParseGraph."""

    _graph: graph.ParseGraph
    debug: bool
    ignore_asserts: bool

    def __init__(
        self,
        input_graph: graph.ParseGraph,
        *,
        debug: bool = False,
        ignore_asserts: bool | None = None,
        monitoring_level: MonitoringLevel = MonitoringLevel.AUTO,
        with_http_server: bool = False,
        default_logging: bool = True,
        persistence_config: PersistenceConfig | None = None,
    ) -> None:
        self._graph = input_graph
        self.debug = debug
        if ignore_asserts is None:
            ignore_asserts = environ.ignore_asserts
        self.ignore_asserts = ignore_asserts
        self.monitoring_level = monitoring_level
        self.with_http_server = with_http_server
        self.default_logging = default_logging
        self.persistence_config = persistence_config or environ.get_replay_config()

    def run_tables(
        self,
        *tables: table.Table,
        after_build: Callable[[ScopeState], None] | None = None,
    ) -> list[api.CapturedTable]:
        nodes, columns = self.tree_shake_tables(self._graph.global_scope, tables)
        context = ScopeContext(nodes, columns)
        return self._run(context, output_tables=tables, after_build=after_build)

    def run_all(
        self, after_build: Callable[[ScopeState], None] | None = None
    ) -> list[api.CapturedTable]:
        context = ScopeContext(nodes=self._graph.global_scope.nodes, run_all=True)
        return self._run(context, after_build=after_build)

    def run_outputs(self, after_build: Callable[[ScopeState], None] | None = None):
        tables = (node.table for node in self._graph.global_scope.output_nodes)
        nodes, columns = self._tree_shake(
            self._graph.global_scope, self._graph.global_scope.output_nodes, tables
        )
        context = ScopeContext(nodes=nodes, columns=columns)
        return self._run(context, after_build=after_build)

    def _run(
        self,
        context: ScopeContext,
        output_tables: Iterable[table.Table] = (),
        after_build: Callable[[ScopeState], None] | None = None,
    ) -> list[api.CapturedTable]:
        storage_graph = OperatorStorageGraph.from_scope_context(
            context, self, output_tables
        )

        def logic(
            scope: api.Scope,
            storage_graph: OperatorStorageGraph = storage_graph,
            context: ScopeContext = context,
        ) -> list[tuple[api.Table, list[ColumnPath]]]:
            state = ScopeState(scope)
            storage_graph.build_scope(scope, state, self)
            if after_build is not None:
                after_build(state)
            return storage_graph.get_output_tables(output_tables, context, state)

        node_names = [
            (operator.id, operator.label())
            for operator in context.nodes
            if isinstance(operator, ContextualizedIntermediateOperator)
        ]
        monitoring_level = self.monitoring_level.to_internal()

        with new_event_loop() as event_loop, monitor_stats(
            monitoring_level, node_names, self.default_logging
        ) as stats_monitor:
            if self.persistence_config:
                self.persistence_config.on_before_run()
                persistence_engine_config = self.persistence_config.engine_config
            else:
                persistence_engine_config = None

            try:
                return api.run_with_new_graph(
                    logic,
                    event_loop=event_loop,
                    ignore_asserts=self.ignore_asserts,
                    stats_monitor=stats_monitor,
                    monitoring_level=monitoring_level,
                    with_http_server=self.with_http_server,
                    persistence_config=persistence_engine_config,
                )
            except api.EngineErrorWithTrace as e:
                error, frame = e.args
                if frame is not None:
                    trace.add_pathway_trace_note(
                        error,
                        trace.Frame(
                            filename=frame.file_name,
                            line_number=frame.line_number,
                            line=frame.line,
                            function=frame.function,
                        ),
                    )
                raise error

    def tree_shake_tables(
        self, graph_scope: graph.Scope, tables: Iterable[table.Table]
    ) -> tuple[StableSet[Operator], StableSet[column.Column]]:
        starting_nodes: Iterable[Operator] = (
            table._source.operator for table in tables
        )
        return self._tree_shake(graph_scope, starting_nodes, tables)

    def _tree_shake(
        self,
        graph_scope: graph.Scope,
        starting_nodes: Iterable[Operator],
        tables: Iterable[table.Table],
    ) -> tuple[StableSet[Operator], StableSet[column.Column]]:
        if self.debug:
            starting_nodes = (*starting_nodes, *graph_scope.debug_nodes)
            tables = (*tables, *(node.table for node in graph_scope.debug_nodes))
        nodes = StableSet(graph_scope.relevant_nodes(*starting_nodes))
        columns = self._relevant_columns(nodes, tables)
        return nodes, columns

    def _relevant_columns(
        self, nodes: Iterable[Operator], tables: Iterable[table.Table]
    ) -> StableSet[column.Column]:
        tables = StableSet.union(
            tables, *(node.hard_table_dependencies() for node in nodes)
        )

        leaf_columns = (table._columns.values() for table in tables)
        id_columns = (
            table._id_column for node in nodes for table in node.output_tables
        )

        stack: list[column.Column] = list(StableSet.union(id_columns, *leaf_columns))
        visited: StableSet[column.Column] = StableSet()

        while stack:
            column = stack.pop()
            if column in visited:
                continue
            visited.add(column)
            for dependency in column.column_dependencies():
                if dependency not in visited:
                    stack.append(dependency)

        return visited


__all__ = [
    "GraphRunner",
]