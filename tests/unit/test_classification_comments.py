"""Each no-translation run leaves a new visible result in the PR timeline."""
from ydbdoc_review_ng.reporting import CLASSIFICATION_MARKER, Comment, report_classification


class Comments:
    def __init__(self):
        self.comments = [Comment(1, True, "Previous result\n" + CLASSIFICATION_MARKER)]
        self.updates = []

    def list_comments(self, pr_number):
        return tuple(self.comments)

    def create_comment(self, pr_number, body):
        self.comments.append(Comment(len(self.comments) + 1, True, body))

    def update_comment(self, pr_number, comment_id, body):
        self.updates.append((comment_id, body))
        self.comments = [
            Comment(item.id, item.authored_by_publisher, body) if item.id == comment_id else item
            for item in self.comments
        ]


def test_each_no_translation_run_creates_new_comment_and_preserves_history():
    backend = Comments()
    original = backend.comments[0]
    report_classification(backend, 52478, reason="Обе версии согласованы.")
    report_classification(backend, 52478, reason="Обе версии согласованы.")
    assert len(backend.comments) == 3
    assert backend.comments[0] == original
    assert backend.updates == []
    for comment in backend.comments[1:]:
        assert comment.body == "Перевод не требуется.\n" + CLASSIFICATION_MARKER


def test_technical_failure_keeps_existing_update_behavior():
    backend = Comments()
    report_classification(backend, 52478)
    assert len(backend.comments) == 1
    assert len(backend.updates) == 1
    assert "классификация изменений не удалась" in backend.comments[0].body
