import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from odin.db import engine, session
from odin.models import User
from odin.security import hasher
from odin.main import app

PASSWORD = 'temporary-password-1234'


@pytest.fixture(autouse=True)
def clean_database():
    # Tests are only invoked against a disposable database named odin_test.
    assert engine().url.database == 'odin_test'
    with engine().begin() as conn:
        conn.execute(text('TRUNCATE telegram_questions, telegram_prompts, telegram_messages, telegram_state, telegram_links, analysis_reviews, analyses, chat_messages, conversations, knowledge, analyst_state, case_evidence, case_notes, cases, correlation_state, log_senders, event_collectors, event_pages, source_events, event_streams, monitoring_destinations, probe_catalogs, probe_origins, probe_maintenance, probe_traces, probe_incident_events, probe_incidents, probe_states, probe_rollups, probe_rounds, probe_series, probe_batches, probe_targets, probe_profile_versions, probe_profiles, inventory_links, reconciliation_candidates, inventory_assignments, inventory_interfaces, source_identities, devices, pops, inventory_runs, users, sessions, auth_challenges, rate_buckets, audit_log, integrations, jobs CASCADE'))
    from odin.probes.config import seed_defaults
    with session() as db:
        seed_defaults(db)
    yield


@pytest.fixture
def client():
    with TestClient(app, base_url='http://localhost:8080', headers={'Origin':'http://localhost:8080'}) as client:
        yield client


@pytest.fixture
def administrator():
    with session() as db:
        u = User(username='admin', role='admin', password_hash=hasher.hash(PASSWORD))
        db.add(u)
        db.commit()
        return u.id
