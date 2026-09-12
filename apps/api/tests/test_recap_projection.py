from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from emer.contracts.http import ConversationView
from emer.domain.recap_policy import RECAP_HASH_PREFIX


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_retired_recap_is_hidden_without_mutating_saved_audit_text(status):
    row = SimpleNamespace(
        id="conversation", title="User title", context_version=1, patient_id=None, as_of=None,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC), pinned=True, title_origin="manual",
        summary="Old unchecked paraphrase", summary_status=status, summary_error="Old rejection",
        summary_input_hash="a" * 64,
    )
    view = ConversationView.model_validate(row)
    assert (view.summary, view.summary_status, view.summary_error) == (None, "idle", None)
    assert view.title == row.title and view.pinned
    assert row.summary == "Old unchecked paraphrase" and row.summary_status == status
    row.summary_input_hash = RECAP_HASH_PREFIX + "b" * 60
    current = ConversationView.model_validate(row)
    assert current.summary_status == status
    assert current.summary == (row.summary if status == "ready" else None)
