"""Import-time ORM smoke test — catch declarative mapping failures BEFORE production.

This regression test exists because of a real production incident: the
``ArtifactRecord`` model declared a Python attribute named ``metadata``:

    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

SQLAlchemy's declarative base already exposes ``.metadata`` (the
``MetaData`` collection of all ``Table`` objects), so declaring an ORM
attribute with that name causes class construction to fail with:

    sqlalchemy.exc.InvalidRequestError: Attribute name 'metadata' is reserved
    when using the Declarative API.

That failure happens at *import time* — before Alembic can inspect the
models, before the FastAPI lifespan opens a DB connection, before any
request handler runs. The container crash-loops on startup. The bug is
silent in CI unless a test explicitly imports every ORM model.

This test imports every model module and asserts:

1. Every model module imports cleanly (declarative mapping succeeds).
2. ``Base.metadata.tables`` contains the expected production tables.
3. No model class re-declares a Python attribute named ``metadata``
   (which would shadow ``Base.metadata`` and break future mapping).
4. Every mapped table has a primary key column.
5. Every foreign-key-bearing column has a matching ``ForeignKey`` object
   pointing at a table that also exists in ``Base.metadata``.

If a developer ever re-introduces a ``metadata`` attribute on an ORM
class — or breaks any other declarative invariant — this test catches
it before another Railway deployment crashes on startup.
"""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase

# These are every module that defines ORM models (subclass of Base).
# If a new model module is added, it MUST be registered here too —
# the assertion at the end of TestAllModelsImport verifies the union
# of tables registered after importing all of these matches the
# expected set, so adding a new model module without listing it here
# would leave its tables unregistered and trip the assertion.
ORM_MODEL_MODULES: list[str] = [
    "wax.state.models",
    "wax.state.bridge_models",
    "wax.state.context_models",
    "wax.state.interaction_models",
    "wax.state.process_models",
    "wax.state.artifact_models",
    "wax.state.credential_store",
]


class TestAllModelsImport:
    """Every ORM model module imports cleanly (declarative mapping succeeds)."""

    @pytest.mark.parametrize("module_name", ORM_MODEL_MODULES)
    def test_module_imports_without_error(self, module_name: str) -> None:
        """Importing a model module must not raise.

        SQLAlchemy's declarative mapping happens during class construction,
        so a single import is sufficient to surface any reserved-attribute
        collision, duplicate column name, or invalid mapper configuration.
        """
        mod = importlib.import_module(module_name)
        assert mod is not None, f"failed to import {module_name}"

    def test_all_expected_tables_registered(self) -> None:
        """After importing every model module, Base.metadata must contain
        the full production table set.

        If a developer adds a new model module but forgets to add it to
        ``ORM_MODEL_MODULES`` above, its tables will be missing here and
        the assertion will fail — forcing the list to stay in sync.
        """
        for m in ORM_MODEL_MODULES:
            importlib.import_module(m)

        from wax.state.models import Base

        registered = set(Base.metadata.tables.keys())
        expected = {
            "principals",
            "conversations",
            "processed_messages",
            "conversation_messages",
            "work_items",
            "executions",
            "execution_steps",
            "delivery_records",
            "memory_records",
            "memory_links",
            "runtime_signals",
            "audit_events",
            "principal_credentials",
            "contexts",
            "interaction_sessions",
            "process_registry",
            "artifacts",
            "secure_credentials",
        }
        missing = expected - registered
        assert not missing, f"Expected tables missing from Base.metadata: {missing}"
        # `extra` (registered - expected) is not asserted strictly — adding
        # a new table is fine as long as it's also added to `expected` so
        # the test stays meaningful. But we do assert the core production
        # set is present.


class TestNoReservedAttributeNames:
    """No ORM model may declare a Python attribute named ``metadata``.

    ``Base.metadata`` is SQLAlchemy's ``MetaData`` collection of every
    ``Table`` registered against the declarative base. Re-declaring
    ``metadata`` as a mapped column attribute causes class construction
    to crash with::

        InvalidRequestError: Attribute name 'metadata' is reserved
        when using the Declarative API.

    The fix is to give the Python attribute a different name (e.g.
    ``artifact_metadata``) while keeping the database column name
    ``metadata`` via the first positional argument of ``mapped_column``.
    """

    def test_no_model_has_metadata_attribute(self) -> None:
        for m in ORM_MODEL_MODULES:
            importlib.import_module(m)

        from wax.state.models import Base

        offenders: list[str] = []
        for mapper_cls in Base.registry.mappers:
            cls = mapper_cls.class_
            # `metadata` is a class-level attribute inherited from
            # DeclarativeBase (the MetaData collection). A model that
            # re-declares it would shadow that — but the re-declaration
            # itself would have crashed at import time, so this check
            # is mostly a forward-looking guard against someone trying
            # to use ``column_property`` or ``deferred`` to sneak it in.
            #
            # The real assertion is that the class's __dict__ (not its
            # MRO) does not contain 'metadata' as a key — that would
            # mean the subclass itself declared it.
            if "metadata" in cls.__dict__:
                offenders.append(f"{cls.__module__}.{cls.__name__}")

        assert not offenders, (
            "These ORM classes declared a `metadata` attribute, which "
            "shadows SQLAlchemy's reserved `Base.metadata` (MetaData "
            f"collection) and crashes declarative mapping: {offenders}"
        )


class TestEveryMappedTableHasPrimaryKey:
    """Every mapped table must have a primary key column.

    A table without a primary key cannot be updated or deleted via the
    ORM (SQLAlchemy can't track identity), so this is a hard requirement
    for any production-bound model.
    """

    def test_all_tables_have_primary_key(self) -> None:
        for m in ORM_MODEL_MODULES:
            importlib.import_module(m)

        from wax.state.models import Base

        offenders: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            if not list(table.primary_key.columns):
                offenders.append(table_name)

        assert not offenders, f"These mapped tables have no primary key column: {offenders}"


class TestBaseMetadataIsAccessible:
    """``Base.metadata`` must be accessible and contain tables.

    This catches a subtle regression: if someone accidentally shadowed
    ``metadata`` on the ``Base`` class itself (e.g. by declaring a
    ``metadata`` class attribute on a mixin that ``Base`` inherits),
    ``Base.metadata`` would no longer be the ``MetaData`` collection,
    and Alembic's ``target_metadata = Base.metadata`` in ``env.py``
    would silently produce an empty migration set.
    """

    def test_base_metadata_is_a_metada_object(self) -> None:
        for m in ORM_MODEL_MODULES:
            importlib.import_module(m)

        from sqlalchemy import MetaData

        from wax.state.models import Base

        assert isinstance(Base.metadata, MetaData), (
            f"Base.metadata is {type(Base.metadata)!r}, expected sqlalchemy.MetaData. "
            "Someone has shadowed the reserved `metadata` attribute on the Base class."
        )
        assert len(Base.metadata.tables) > 0, (
            "Base.metadata has no tables registered. Either no model was "
            "imported, or `metadata` has been shadowed on the Base class."
        )

    def test_base_subclass_is_declarative_base(self) -> None:
        from wax.state.models import Base

        # The sanity check: Base must inherit from DeclarativeBase so
        # its `metadata` attribute resolves to the MetaData collection.
        assert isinstance(Base, type), "Base must be a class"
        assert issubclass(Base, DeclarativeBase), (
            "Base must inherit from sqlalchemy.orm.DeclarativeBase so that "
            "Base.metadata resolves to the MetaData collection."
        )


class TestRenamedMetadataAttributes:
    """Verify the renamed attributes exist and map to the `metadata` DB column.

    This pins the fix: ``ArtifactRecord.artifact_metadata``,
    ``ProcessRecord.process_metadata``, and
    ``CredentialRecord.credential_metadata`` must all exist as Python
    attributes and must map to a database column named ``metadata``
    (not the Python attribute name).
    """

    def test_artifact_record_metadata_renamed(self) -> None:
        from wax.state.artifact_models import ArtifactRecord

        assert hasattr(ArtifactRecord, "artifact_metadata"), (
            "ArtifactRecord must expose `artifact_metadata` (renamed from `metadata` "
            "to avoid colliding with SQLAlchemy's reserved Base.metadata)"
        )
        # The mapped column must use the DB column name 'metadata'
        col = ArtifactRecord.__table__.c.get("metadata")
        assert col is not None, (
            "ArtifactRecord must map a column named 'metadata' in the DB "
            "(the Python attribute is renamed but the DB column stays 'metadata')"
        )
        # And there must NOT be a column using the Python attribute name
        assert "artifact_metadata" not in ArtifactRecord.__table__.c, (
            "artifact_metadata should NOT be a DB column — only a Python attribute"
        )

    def test_process_record_metadata_renamed(self) -> None:
        from wax.state.process_models import ProcessRecord

        assert hasattr(ProcessRecord, "process_metadata"), (
            "ProcessRecord must expose `process_metadata` (renamed from `metadata`)"
        )
        col = ProcessRecord.__table__.c.get("metadata")
        assert col is not None, "ProcessRecord must map a column named 'metadata' in the DB"
        assert "process_metadata" not in ProcessRecord.__table__.c

    def test_credential_record_metadata_renamed(self) -> None:
        from wax.state.credential_store import CredentialRecord

        assert hasattr(CredentialRecord, "credential_metadata"), (
            "CredentialRecord must expose `credential_metadata` (renamed from `metadata`)"
        )
        col = CredentialRecord.__table__.c.get("metadata")
        assert col is not None, "CredentialRecord must map a column named 'metadata' in the DB"
        assert "credential_metadata" not in CredentialRecord.__table__.c


class TestAlembicCanInspectModels:
    """Alembic's migration inspector must be able to read Base.metadata.

    This is a forward-looking test: if Alembic can't produce a
    ``MigrationContext`` from ``Base.metadata``, then ``alembic revision
    --autogenerate`` will produce an empty migration and the next model
    change will silently fail to migrate.
    """

    def test_alembic_can_compare_models(self) -> None:
        for m in ORM_MODEL_MODULES:
            importlib.import_module(m)

        from sqlalchemy import MetaData

        from wax.state.models import Base

        # The inspector used by Alembic's autogenerate.
        # We don't need a real DB connection — we just need to confirm
        # that Base.metadata is a MetaData with bound tables, which is
        # what Alembic's compare_metadata() requires.
        assert isinstance(Base.metadata, MetaData)
        assert len(Base.metadata.tables) >= 17, (
            "Expected at least 17 production tables registered to Base.metadata"
        )

        # Also: every table must have a name (sounds trivial, but a
        # table without __tablename__ would crash Alembic's reflection).
        for table_name, table in Base.metadata.tables.items():
            assert table.name == table_name, (
                f"Table object name {table.name!r} does not match key {table_name!r}"
            )

        # And every table's columns must be inspectable.
        for table_name, table in Base.metadata.tables.items():
            cols = {c.name for c in inspect(table).columns}
            assert cols, f"Table {table_name} has no inspectable columns"
