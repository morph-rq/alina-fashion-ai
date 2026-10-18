from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import AuditLog, Customer, ImportBatch, Season


def test_delete_stored_import_file_retains_history(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    source = tmp_path / "data" / "imports" / "master" / "batch-1" / "supplier.xlsx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"test workbook copy")

    with factory() as session:
        batch = ImportBatch(
            filename=source.name,
            sha256="a" * 64,
            stored_path="data/imports/master/batch-1/supplier.xlsx",
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id

    def override_db():
        with factory() as session:
            yield session

    monkeypatch.setattr("app.main.ROOT_DIR", tmp_path)
    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).post(
            f"/imports/{batch_id}/source/delete",
            data={"confirmation": "DELETE_SOURCE_FILE"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    assert not source.exists()
    with factory() as session:
        retained = session.get(ImportBatch, batch_id)
        assert retained is not None
        assert retained.source_deleted_at is not None
        assert session.scalar(select(func.count(AuditLog.id))) == 1


def test_delete_stored_import_rejects_path_outside_managed_directory(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"must survive")
    with factory() as session:
        batch = ImportBatch(filename=outside.name, sha256="b" * 64, stored_path="outside.xlsx")
        session.add(batch)
        session.commit()
        batch_id = batch.id

    def override_db():
        with factory() as session:
            yield session

    monkeypatch.setattr("app.main.ROOT_DIR", tmp_path)
    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).post(
            f"/imports/{batch_id}/source/delete",
            data={"confirmation": "DELETE_SOURCE_FILE"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert outside.exists()


def test_reset_all_removes_database_records_and_managed_files_only(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    managed_files = [
        tmp_path / "data" / "imports" / "master" / "batch-1" / "supplier.xlsx",
        tmp_path / "data" / "images" / "import-1" / "product.jpg",
        tmp_path / "output" / "pdf" / "AA_SS26_statement.pdf",
        tmp_path / "output" / "pdf" / "SS26_customer_statements.zip",
        tmp_path / "data" / "reports" / "fresh-statement.pdf",
    ]
    for path in managed_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"managed application data")
    outside = tmp_path / "originals" / "supplier.xlsx"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"original outside managed directories")

    with factory() as session:
        session.add_all(
            [
                Season(name="SS26"),
                Customer(code="AA", name="Customer AA"),
                ImportBatch(filename="supplier.xlsx", sha256="c" * 64, stored_path="data/imports/master/batch-1/supplier.xlsx"),
                AuditLog(entity_type="ImportBatch", entity_id="1", source_user="test", description="Imported supplier workbook"),
            ]
        )
        session.commit()

    def override_db():
        with factory() as session:
            yield session

    monkeypatch.setattr("app.main.ROOT_DIR", tmp_path)
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        preview = client.get("/workspace/reset")
        assert preview.status_code == 200
        assert "audit history" in preview.text and "audit_logs: 1" in preview.text and "customers: 1" in preview.text
        assert client.post("/imports/reset-all", data={"confirmation": "DELETE ALL"}, follow_redirects=False).headers["location"] == "/workspace/reset"
        rejected = client.post("/workspace/reset", data={"confirmation": "DELETE ALL"}, follow_redirects=False)
        assert rejected.status_code == 422
        assert all(path.exists() for path in managed_files)

        response = client.post("/workspace/reset", data={"confirmation": "RESET WORKSPACE"}, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    snapshots = list((tmp_path / "backups" / "recovery").glob("snapshot-*.zip"))
    assert len(snapshots) == 1 and snapshots[0].name in response.headers["location"]
    from app.recovery import restore_snapshot
    restored = restore_snapshot(snapshots[0], tmp_path / "restore-check")
    assert restored["table_counts"]["customers"] == 1 and restored["table_counts"]["audit_logs"] == 1
    assert (tmp_path / "restore-check" / "data" / "imports" / "master" / "batch-1" / "supplier.xlsx").exists()
    assert outside.exists()
    assert all(not path.exists() for path in managed_files)
    assert not any((tmp_path / "data" / "imports").iterdir())
    assert not any((tmp_path / "data" / "images").iterdir())
    assert not any((tmp_path / "data" / "reports").iterdir())
    assert not any((tmp_path / "output" / "pdf").iterdir())
    with factory() as session:
        assert session.scalar(select(func.count(Season.id))) == 0
        assert session.scalar(select(func.count(Customer.id))) == 0
        assert session.scalar(select(func.count(ImportBatch.id))) == 0
        assert session.scalar(select(func.count(AuditLog.id))) == 0
