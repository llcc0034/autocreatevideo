"""Exact frozen execution references shared by navigation and deletion review."""
from django.db.models import BooleanField, Func


class RecipeReference(Func):
    """Only schema3 stage routes reference execution versions.

    A UUID inside an input literal, description or an old resource route is not
    an execution choice. Summary and drill-down share this bounded SQL predicate.
    """
    output_field = BooleanField()

    def as_sqlite(self, compiler, connection, **extra_context):
        definition, dp = compiler.compile(self.source_expressions[0])
        identifier, ip = compiler.compile(self.source_expressions[1])
        return (f"(json_extract({definition}, '$.schema_version')=3 AND EXISTS ("
                f"SELECT 1 FROM json_each({definition}, '$.stages') s "
                "JOIN json_each(s.value, '$.routing.routes') r "
                f"WHERE json_extract(r.value, '$.execution_instance_version_id')={identifier}))", [*dp, *dp, *ip])

    def as_postgresql(self, compiler, connection, **extra_context):
        definition, dp = compiler.compile(self.source_expressions[0])
        identifier, ip = compiler.compile(self.source_expressions[1])
        return (f"(({definition} -> 'schema_version')='3'::jsonb AND "
                f"jsonb_path_exists({definition}, %s::jsonpath, jsonb_build_object('target', {identifier})))",
                [*dp, *dp, '$.stages[*].routing.routes[*].execution_instance_version_id ? (@ == $target)', *ip])
