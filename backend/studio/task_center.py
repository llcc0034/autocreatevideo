"""Narrow, permission-scoped projections. No queue, facts, or state writes."""
from hashlib import sha256
import json

from django.core import signing
from django.db.models import (BooleanField, Case, CharField, Count, DateTimeField, DecimalField,
                             Exists, F, IntegerField, OuterRef, Q, Subquery, UUIDField, Value, When)
from django.db.models.functions import Coalesce, Greatest, NullIf
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.utils.urls import replace_query_param, remove_query_param

from studio.models import (AssemblyJob, AssetGenerationJob, ExecutionPreparation, GenerationAttempt,
                           GenerationTask, ModelDeploymentSmokeAttempt, ModelExecutionInstanceVersion,
                           SeriesProject, WorkflowSmokeAttempt)
from studio.services.execution_preparation_owners import visible_execution_preparations


ROW_FIELDS = ("kind", "id", "category", "title", "status", "operation", "project_id", "project_title",
              "production_run_id", "execution_instance_version_id", "execution_instance_name", "execution_version",
              "preparation_id", "source_id", "source_kind", "created_at", "updated_at", "finished_at",
              "estimated_cost", "actual_cost", "cost_currency", "anomaly")
CATEGORIES = (("production", "项目生产"), ("asset", "素材生成"), ("smoke", "Smoke 验收"))


def null(field):
    return Value(None, output_field=field)


def text(value):
    return Value(value, output_field=CharField())


def root_preparations(actor):
    leaf = GenerationAttempt.objects.filter(preparation_id=OuterRef("pk"), role="auxiliary")
    return (visible_execution_preparations(actor).filter(asset_job__parent_preparation__isnull=True)
            .exclude(stage_run__isnull=False, unit_key__startswith="field:").exclude(Exists(leaf)))


def owner_rows(model, actor):
    query = model.objects.all()
    if not actor or not actor.is_authenticated or not actor.is_active:
        return query.none()
    return query if actor.is_staff else query.filter(owner=actor)


def orphan_tasks(actor):
    return owner_rows(GenerationTask, actor).exclude(Exists(
        GenerationAttempt.objects.filter(generation_task_id=OuterRef("pk"), preparation__isnull=False)))


def primary_attempts(preparation_id):
    return GenerationAttempt.objects.filter(preparation_id=preparation_id).order_by(
        "-attempt_number", "-created_at", "-id")


def row_queries(actor, include_auxiliary=False):
    primary = primary_attempts(OuterRef("pk"))
    preparations = visible_execution_preparations(actor) if include_auxiliary else root_preparations(actor)
    prep = preparations.annotate(**{
        "_main_" + key: Subquery(primary.values(key)[:1])
        for key in ("id", "status", "operation", "generation_task_id", "finished_at", "updated_at",
                    "estimated_cost", "actual_cost", "cost_currency")})
    owner_prefixes = ("asset_job", "deployment_smoke", "workflow_smoke")
    def source(field, output_field=None):
        return Coalesce(F("_main_" + field), *(F(prefix + "__" + field) for prefix in owner_prefixes),
                        output_field=output_field)
    category = Case(When(stage_run__isnull=False, then=text("production")),
                    When(asset_job__isnull=False, then=text("asset")), default=text("smoke"))
    operation = Coalesce(NullIf(F("_main_operation"), text("")), F("asset_job__operation"),
                         F("deployment_smoke__operation"), F("execution_instance_version__operation"),
                         # Resolve the relation before extracting JSON. KT(path) treats
                         # its first segment as the JSON column (here a UUID FK).
                         KeyTextTransform("operation", F("workflow_smoke__fixture_values__resource_definition__value__configuration")), text(""),
                         output_field=CharField())
    prep_values = dict(kind=text("preparation"), id=F("id"), category=category,
        title=Coalesce(NullIf(F("asset_job__name"), text("")), F("execution_instance_version__instance__name"),
                       F("deployment_smoke__deployment__name"), F("workflow_smoke__workflow_version__template__name"),
                       F("stage_run__stage_key"), text("执行准备"), output_field=CharField()),
        status=Coalesce(source("status", CharField()), F("status"), output_field=CharField()), operation=operation,
        project_id=F("stage_run__production_run__project_id"),
        project_title=Coalesce(F("stage_run__production_run__project__title"), text("")),
        production_run_id=F("stage_run__production_run_id"),
        execution_instance_version_id=F("execution_instance_version_id"),
        execution_instance_name=Coalesce(F("execution_instance_version__instance__name"), text("")),
        execution_version=F("execution_instance_version__version"), preparation_id=F("id"),
        source_id=Coalesce(F("_main_id"), F("asset_job_id"), F("deployment_smoke_id"), F("workflow_smoke_id")),
        source_kind=Case(When(_main_id__isnull=False, then=text("generation_attempt")),
            When(asset_job__isnull=False, then=text("asset_job")), When(deployment_smoke__isnull=False, then=text("deployment_smoke")),
            When(workflow_smoke__isnull=False, then=text("workflow_smoke")), default=text("preparation")),
        created_at=F("created_at"), updated_at=Greatest(F("updated_at"), Coalesce(source("updated_at"), F("updated_at"))),
        finished_at=source("finished_at"), estimated_cost=source("estimated_cost"), actual_cost=source("actual_cost"),
        cost_currency=Coalesce(source("cost_currency"), text("")),
        anomaly=Case(When(stage_run__isnull=False, status="dispatched", _main_id__isnull=True, then=Value(True)),
                     default=Value(False), output_field=BooleanField()))
    prep = prep.annotate(**{"tc_" + key: val for key, val in prep_values.items()})
    queries = {"preparation": prep}
    for kind, query in (("generation_task", orphan_tasks(actor)), ("assembly", owner_rows(AssemblyJob, actor))):
        values = dict(kind=text(kind), id=F("id"), category=text("production"), title=F("project__title"),
            status=F("status"), operation=text("assembly" if kind == "assembly" else ""),
            project_id=F("project_id"), project_title=F("project__title"), production_run_id=null(UUIDField()),
            execution_instance_version_id=null(UUIDField()), execution_instance_name=text(""), execution_version=null(IntegerField()),
            preparation_id=null(UUIDField()), source_id=F("id"), source_kind=text(kind), created_at=F("created_at"),
            updated_at=F("updated_at"), finished_at=F("finished_at"), estimated_cost=null(DecimalField()),
            actual_cost=null(DecimalField()), cost_currency=text(""), anomaly=Value(kind == "generation_task", output_field=BooleanField()))
        if kind == "assembly":
            values["production_run_id"] = F("consumer_stage__production_run_id")
        queries[kind] = query.annotate(**{"tc_" + key: val for key, val in values.items()})
    return queries


def filtered_queries(actor, filters):
    queries = row_queries(actor)
    for kind, query in queries.items():
        if "unit_key" in filters:
            query = (query.filter(stage_run__isnull=False, unit_key=filters["unit_key"])
                     if kind == "preparation" else query.none())
        scope = {key: filters[key] for key in ("production_run_id", "stage_run_id") if key in filters}
        if scope:
            if kind == "preparation":
                query = query.filter(**{("stage_run__production_run_id" if key == "production_run_id" else key): value
                                       for key, value in scope.items()})
            elif kind == "assembly":
                # Creation facts survive explicit failed-job replacement. Never
                # infer the consumer from the current link or a source clip.
                query = query.filter(**{("consumer_stage__production_run_id" if key == "production_run_id"
                    else "consumer_stage_id"): value for key, value in scope.items()})
            else:
                # An orphan task has no preparation membership. Only an actual
                # attempt relation may establish its production scope.
                members = GenerationAttempt.objects.filter(**{
                    ("stage_run__production_run_id" if key == "production_run_id" else key): value for key, value in scope.items()})
                query = query.filter(pk__in=members.order_by().values("generation_task_id"))
        for key in ("category", "status", "project_id", "execution_instance_version_id", "operation", "anomaly"):
            if key in filters:
                query = query.filter(**{"tc_" + key: filters[key]})
        for key, suffix in (("created_after", "gte"), ("created_before", "lte")):
            if key in filters:
                query = query.filter(**{"created_at__" + suffix: filters[key]})
        if "source_id" in filters:
            identifier = filters["source_id"]
            condition = Q(tc_source_id=identifier)
            if kind == "preparation":
                matching_attempt = GenerationAttempt.objects.filter(preparation_id=OuterRef("pk")).filter(
                    Q(pk=identifier) | Q(generation_task_id=identifier))
                condition |= Q(asset_job_id=identifier) | Q(deployment_smoke_id=identifier) | Q(workflow_smoke_id=identifier) | Q(Exists(matching_attempt))
            query = query.filter(condition)
        queries[kind] = query
    return queries


def project_row(row):
    from studio.task_center_public import public_value
    return {key: public_value(row["tc_" + key]) if key in {"title", "project_title", "execution_instance_name"}
            else row["tc_" + key] for key in ROW_FIELDS}


def union_rows(queries):
    selected = [query.order_by().values(*("tc_" + key for key in ROW_FIELDS)) for query in queries]
    return selected[0].union(*selected[1:], all=True) if len(selected) > 1 else selected[0]


def list_tasks(actor, filters, request):
    read_at = timezone.now()
    queries = filtered_queries(actor, filters)
    counts = dict(all=0, production=0, asset=0, smoke=0)
    for query in filtered_queries(actor, {key: value for key, value in filters.items() if key != "category"}).values():
        for row in query.order_by().values("tc_category").annotate(total=Count("pk")):
            counts[row["tc_category"]] += row["total"]
            counts["all"] += row["total"]
    scope = sha256(json.dumps({"actor": str(actor.pk), **{key: str(value) for key, value in filters.items() if key != "cursor"}},
                             sort_keys=True).encode()).hexdigest()
    reverse = False
    if filters.get("cursor"):
        try:
            cursor = signing.loads(filters["cursor"], salt="task-center")
            stamp = parse_datetime(cursor["at"])
            if cursor["scope"] != scope or not stamp or cursor["kind"] not in queries:
                raise ValueError
            from uuid import UUID
            identifier = UUID(cursor["id"])
            reverse = cursor["reverse"]
            if type(reverse) is not bool:
                raise ValueError
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            raise ValidationError({"cursor": "游标无效或筛选范围已改变"})
        compare, kind_compare = ("gt", "lt") if reverse else ("lt", "gt")
        for kind, query in queries.items():
            condition = Q(**{"tc_created_at__" + compare: stamp}) | Q(tc_created_at=stamp, **{"tc_kind__" + kind_compare: cursor["kind"]})
            condition |= Q(tc_created_at=stamp, tc_kind=cursor["kind"], **{"tc_id__" + compare: identifier})
            queries[kind] = query.filter(condition)
    order = ("tc_created_at", "-tc_kind", "tc_id") if reverse else ("-tc_created_at", "tc_kind", "-tc_id")
    size = filters["page_size"]
    rows = list(union_rows(list(queries.values())).order_by(*order)[:size + 1])
    more = len(rows) > size
    rows = rows[:size]
    if reverse:
        rows.reverse()
    def link(row, backwards):
        token = signing.dumps({"at": row["tc_created_at"].isoformat(), "kind": row["tc_kind"], "id": str(row["tc_id"]),
                               "reverse": backwards, "scope": scope}, salt="task-center", compress=True)
        return replace_query_param(request.get_full_path(), "cursor", token)
    return {"count": counts[filters.get("category", "all")], "counts": counts, "read_at": read_at, "results": [project_row(row) for row in rows],
            "next": link(rows[-1], False) if rows and (bool(filters.get("cursor")) if reverse else more) else None,
            "previous": link(rows[0], True) if rows and (more if reverse else bool(filters.get("cursor"))) else None}


def detail_row(actor, kind, identifier):
    queries = row_queries(actor, include_auxiliary=True)
    if kind not in queries:
        raise NotFound()
    row = queries[kind].filter(pk=identifier).values(*("tc_" + key for key in ROW_FIELDS)).first()
    if row is None:
        raise NotFound()
    return project_row(row)


def page_links(request, key, page, size, count):
    path = request.get_full_path()
    for name in ("content_field", "content_offset", "content_limit"):
        path = remove_query_param(path, name)
    return (replace_query_param(path, key, page + 1) if page * size < count else None,
            replace_query_param(path, key, page - 1) if page > 1 else None)


def options(actor, query, request):
    from studio.services.model_profile_options import MODEL_CAPABILITY_CATALOG
    from studio.task_center_public import public_value
    preparations = root_preparations(actor)
    projects = SeriesProject.objects.filter(Q(pk__in=preparations.values("stage_run__production_run__project_id")) |
        Q(pk__in=orphan_tasks(actor).values("project_id")) | Q(pk__in=owner_rows(AssemblyJob, actor).values("project_id")))
    versions = ModelExecutionInstanceVersion.objects.filter(pk__in=preparations.values("execution_instance_version_id"))
    labels = {}
    for model in (ExecutionPreparation, GenerationAttempt, AssetGenerationJob, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt, GenerationTask, AssemblyJob):
        labels.update(dict(model.Status.choices))
    # Shared call states use the generic execution vocabulary, not the last
    # queue's domain-specific label (e.g. AssemblyJob's "组装中").
    labels.update(dict(GenerationAttempt.Status.choices))
    result = {"categories": [{"value": key, "label": label} for key, label in CATEGORIES if key != "smoke" or actor.is_staff],
              "statuses": [{"value": key, "label": label} for key, label in labels.items()],
              "operations": list(MODEL_CAPABILITY_CATALOG) + [{"value": "assembly", "label": "成片组装"}],
              "projects": [], "execution_versions": [], "pagination": {}}
    page, size, search = query["page"], query["page_size"], query["search"]
    for domain, rows in (("projects", projects), ("execution_versions", versions)):
        if query["domain"] not in {"all", domain}:
            continue
        field = "title" if domain == "projects" else "instance__name"
        rows = rows.filter(**{field + "__icontains": search}).order_by(field, "id")
        count = rows.count()
        columns = ("id", field) if domain == "projects" else ("id", field, "version")
        result[domain] = [{"value": str(row["id"]), "label": public_value(row[field]) +
                          (f" · v{row['version']}" if domain == "execution_versions" else "")}
                         for row in rows.values(*columns)[(page - 1) * size:page * size]]
        nxt, prev = page_links(request, "page", page, size, count)
        result["pagination"][domain] = {"count": count, "next": replace_query_param(nxt, "domain", domain) if nxt else None,
                                        "previous": replace_query_param(prev, "domain", domain) if prev else None}
    return result
