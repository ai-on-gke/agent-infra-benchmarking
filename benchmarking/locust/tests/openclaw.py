import os

if os.environ.get("LOCUST_NO_OPENCLAW_USER") != "1":
    from locust import User, task
    from common.boomer_config import init_boomer_config

    init_boomer_config()

    class OpenClawUser(User):
        host = "api.ate-system.svc.cluster.local:443"

        @task
        def noop(self) -> None:
            pass
