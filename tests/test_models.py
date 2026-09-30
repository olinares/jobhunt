from jobhunt.models import BoardRef, Job


def test_board_key_standard_ats():
    assert BoardRef("ashby", "acme").key() == "ashby:acme"


def test_board_key_workday_uses_host_and_site():
    board = BoardRef("workday", "acme", host="acme.wd5.myworkdayjobs.com", site="External")
    assert board.key() == "workday:acme.wd5.myworkdayjobs.com/External"


def test_job_uid_combines_board_and_external_id():
    job = Job(BoardRef("lever", "acme"), "123", "Solutions Engineer", "Acme", "https://x")
    assert job.uid == "lever:acme:123"
