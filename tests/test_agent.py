"""H. The assistant uses real tools, asks when unsure, and changes nothing without approval."""
from __future__ import annotations

import json
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from test_factory_settlement import db, imported  # noqa: F401
from app import agent
from app.customer_documents import customer_snapshot
from app.models import AgentProposal, Customer, CustomerPricingRule, GeneratedDocument, OrderItem, Payment, Season


@pytest.fixture()
def docs_root(tmp_path, monkeypatch):
    from app import customer_documents
    real = customer_documents.generate
    monkeypatch.setattr(customer_documents, "generate", lambda *a, **k: real(*a, **{**k, "root": tmp_path}))
    return tmp_path


def test_offline_ids_35_excel_pdf_end_to_end(db, tmp_path, docs_root):
    imported(db, tmp_path)
    turn = agent.handle(db, "c1", "Выдели IDS, поставь 35%, сделай Excel и PDF.")
    names = [c["name"] for c in turn.tool_calls]
    assert names[:2] == ["propose_changes", "prepare_documents"]
    assert turn.proposal and turn.proposal["status"] == "pending"
    assert any("IDS" in m and "35" in m for m in turn.proposal["will_do"])
    assert db.scalar(select(CustomerPricingRule)) is None                     # nothing saved yet
    result = agent.commit_proposal(db, turn.proposal["id"])
    assert {d["format"] for d in result["documents"]} == {"xlsx", "pdf"}
    coat = db.scalar(select(OrderItem).where(OrderItem.article == "TEST-COAT-01"))
    assert coat.markup_percentage == D("0.35")
    assert db.scalar(select(GeneratedDocument).where(GeneratedDocument.file_format == "pdf")).customer.code == "IDS"
    again = agent.commit_proposal(db, turn.proposal["id"])
    assert again.get("already") == "committed"                                # a second click does nothing


def test_offline_several_rates_and_dative_name(db, tmp_path):
    imported(db, tmp_path)
    turn = agent.handle(db, "c2", "Сделай IDS 35%, AA 30%, Алине 25%.")
    actions = json.loads(db.scalar(select(AgentProposal)).actions_json)["actions"]
    assert sorted((a["params"]["customer"], a["params"]["markup"]) for a in actions[:2]) == [("AA", "30"), ("IDS", "35")]
    # Alina is marked as the owner: that part is explained and skipped, never silently applied.
    assert len(actions) == 2 and "владелец" in turn.reply
    assert db.scalar(select(CustomerPricingRule)) is None


def test_offline_questions_settlement_and_deposits(db, tmp_path):
    imported(db, tmp_path)
    owed = agent.handle(db, "c3", "Сколько мы должны фабрике сейчас?")
    assert "calculate_factory_settlement" in [c["name"] for c in owed.tool_calls]
    assert "€10 530.00" in owed.reply and owed.proposal is None
    each = agent.handle(db, "c3", "Сколько каждый должен внести за 50% предоплаты?")
    assert "IDS" in each.reply and "Ваш взнос" in each.reply


def test_payment_needs_confirmation_question_then_follow_up(db, tmp_path):
    imported(db, tmp_path)
    turn = agent.handle(db, "c4", "Учти, что AA уже оплатила €5,000.")
    assert turn.question and "поступили" in turn.reply and turn.proposal is None
    follow = agent.handle(db, "c4", "Поступили фабрике")
    assert follow.proposal and db.scalar(select(Payment)) is None
    agent.commit_proposal(db, follow.proposal["id"])
    payment = db.scalar(select(Payment))
    assert payment.amount == D("5000.00") and payment.destination == "factory" and payment.status == "confirmed"


def test_cancel_proposal_changes_nothing(db, tmp_path):
    imported(db, tmp_path)
    turn = agent.handle(db, "c5", "IDS 40%")
    agent.cancel_proposal(db, turn.proposal["id"])
    assert db.scalar(select(AgentProposal)).status == "cancelled"
    assert db.scalar(select(CustomerPricingRule)) is None
    with pytest.raises(ValueError):
        agent.commit_proposal(db, "missing")


def test_ambiguous_initials_are_asked_not_guessed(db, tmp_path):
    imported(db, tmp_path)
    db.add(Customer(code="ALINAK", name="Alinka"))
    db.commit()
    turn = agent.handle(db, "c6", "Сделай Excel для Алин")
    assert "нескольким" in turn.reply or turn.question
    assert db.scalar(select(GeneratedDocument)) is None


class FakeClaude:
    """Scripted model: proves the loop really executes tools and feeds results back."""

    def __init__(self, script):
        self.script, self.seen = list(script), []

    def complete(self, system, messages, tools):
        self.seen.append(messages[-1])
        step = self.script.pop(0)
        return {"content": step(messages) if callable(step) else step}


def use(name, payload, id_="t1"):
    return [{"type": "tool_use", "id": id_, "name": name, "input": payload}]


def test_llm_loop_invokes_tools_and_waits_for_approval(db, tmp_path, docs_root):
    imported(db, tmp_path)

    def read_result(messages):
        result = json.loads(messages[-1]["content"][0]["content"])
        assert result["factory_total"] == "10530.00"                       # the model saw the real calculation
        return use("propose_changes", {"summary": "IDS 35%", "actions": [
            {"action": "set_customer_markup", "params": {"customer": "IDS", "markup": "35", "season": "SS27"}}]}, "t2")

    client = FakeClaude([
        use("calculate_factory_settlement", {"season": "SS27"}),
        read_result,
        use("prepare_documents", {"customer": "IDS", "season": "SS27"}, "t3"),
        [{"type": "text", "text": "Готово к подтверждению: IDS 35%, Excel и PDF."}],
    ])
    turn = agent.handle(db, "c7", "Выдели IDS, поставь 35%, сделай Excel и PDF.", {"season": "SS27"}, client=client)
    assert [c["name"] for c in turn.tool_calls] == ["calculate_factory_settlement", "propose_changes", "prepare_documents"]
    assert turn.proposal["documents"] == [{"customer": "IDS", "season": "SS27", "brands": None}]
    assert db.scalar(select(CustomerPricingRule)) is None
    agent.commit_proposal(db, turn.proposal["id"])
    assert db.scalar(select(CustomerPricingRule)).markup == D("0.35")
    snap = customer_snapshot(db, db.scalar(select(Customer).where(Customer.code == "IDS")), db.scalar(select(Season)))
    assert snap["total"] == "3037.50"


def test_llm_bad_action_is_reported_back_not_saved(db, tmp_path):
    imported(db, tmp_path)
    client = FakeClaude([
        use("propose_changes", {"summary": "x", "actions": [{"action": "set_customer_markup", "params": {"customer": "ZZ", "markup": "35"}}]}),
        lambda m: (_ for _ in ()).throw(AssertionError) if "не найден" not in m[-1]["content"][0]["content"] else
        use("ask_user", {"question": "Кто такой ZZ?", "options": ["IDS", "AA"]}, "t2"),
    ])
    turn = agent.handle(db, "c8", "ZZ 35%", client=client)
    assert turn.question == {"question": "Кто такой ZZ?", "options": ["IDS", "AA"]} and turn.proposal is None
