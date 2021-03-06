from datetime import datetime
from typing import Any, Mapping, Sequence

from snuba_sdk.conditions import Condition, Op
from snuba_sdk.entity import Entity
from snuba_sdk.expressions import (
    Column,
    Direction,
    Function,
    LimitBy,
    OrderBy,
)
from snuba_sdk.query import Query
from snuba_sdk.query_visitors import InvalidQuery


def parse_datetime(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%f")


def parse_scalar(value: Any) -> Any:
    if isinstance(value, list):
        return list(map(parse_scalar, value))
    elif isinstance(value, tuple):
        return tuple(map(parse_scalar, value))

    if isinstance(value, str):
        try:
            date_scalar = parse_datetime(value)
            return date_scalar
        except ValueError:
            return value

    return value


def parse_exp(value: Any) -> Any:
    if isinstance(value, str):
        return Column(value)
    if not isinstance(value, list):
        return parse_scalar(value)

    alias = value[2] if len(value) > 2 else None

    if value[0].endswith("()") and not value[1]:
        return Function(value[0].strip("()"), [], alias)

    children = None
    if isinstance(value[1], list):
        children = list(map(parse_exp, value[1]))
    elif value[1]:
        children = [parse_exp(value[1])]

    return Function(value[0], children, alias)


def json_to_snql(body: Mapping[str, Any], entity: str) -> Query:
    dataset = body.get("dataset", "")
    sample = body.get("sample")
    query = Query(dataset, Entity(entity, sample))

    selected_columns = list(map(parse_exp, body.get("selected_columns", [])))
    for a in body.get("aggregations", []):
        selected_columns.append(parse_exp(a))

    arrayjoin = body.get("arrayjoin")
    if arrayjoin:
        selected_columns.append(Function("arrayJoin", [Column(arrayjoin)], arrayjoin))

    query = query.set_select(selected_columns)

    groupby = body.get("groupby", [])
    if groupby and not isinstance(groupby, list):
        groupby = [groupby]

    query = query.set_groupby(list(map(parse_exp, groupby)))

    conditions = []
    for cond in body.get("conditions", []):
        if len(cond) != 3 or not isinstance(cond[1], str):
            raise InvalidQuery("OR conditions not supported yet")

        conditions.append(
            Condition(parse_exp(cond[0]), Op(cond[1]), parse_scalar(cond[2]))
        )

    extra_conditions = [("project", "project_id"), ("organization", "org_id")]
    for cond, col in extra_conditions:
        column = Column(col)
        values = body.get(cond)
        if isinstance(values, int):
            conditions.append(Condition(column, Op.EQ, values))
        elif isinstance(values, list):
            rhs: Sequence[Any] = list(map(parse_scalar, values))
            conditions.append(Condition(column, Op.IN, rhs))
        elif isinstance(values, tuple):
            rhs = tuple(map(parse_scalar, values))
            conditions.append(Condition(column, Op.IN, rhs))

    date_conds = [("from_date", Op.GT), ("to_date", Op.LTE)]
    for cond, op in date_conds:
        date_str = body.get(cond, "")
        if date_str:
            # HACK: This is to get sessions working quickly.
            # The time column should depend on the entity.
            conditions.append(
                Condition(Column("started"), op, parse_datetime(date_str))
            )

    query = query.set_where(conditions)

    having = []
    for cond in body.get("having", []):
        if len(cond) != 3 or not isinstance(cond[1], str):
            raise InvalidQuery("OR conditions not supported yet")

        having.append(Condition(parse_exp(cond[0]), Op(cond[1]), parse_scalar(cond[2])))

    query = query.set_having(having)

    order_by = body.get("orderby")
    if order_by:
        if not isinstance(order_by, list):
            order_by = [order_by]

        order_bys = []
        for o in order_by:
            direction = Direction.ASC
            if isinstance(o, str) and o.startswith("-"):
                direction = Direction.DESC
                o = o.lstrip("-")

            order_bys.append(OrderBy(parse_exp(o), direction))

        query = query.set_orderby(order_bys)

    limitby = body.get("limitby")
    if limitby:
        limit, name = limitby
        query = query.set_limitby(LimitBy(Column(name), int(limit)))

    extras = (
        "limit",
        "offset",
        "granularity",
        "totals",
        "consistent",
        "turbo",
        "debug",
    )
    for extra in extras:
        if body.get(extra) is not None:
            query = getattr(query, f"set_{extra}")(body.get(extra))

    return query
