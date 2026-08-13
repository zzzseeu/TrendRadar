import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/project-handover-guide.md"


class ProjectHandoverGuideTests(unittest.TestCase):
    def test_readme_links_to_the_handover_guide(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "[项目接手与运维指南](docs/project-handover-guide.md)",
            readme,
        )

    def test_guide_uses_the_single_current_compose_entrypoint(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn("docker/docker-compose-build.yml", guide)
        self.assertIn("up -d --build --force-recreate", guide)
        self.assertIn(
            "exec trendradar python -m trendradar --force-weekly",
            guide,
        )

    def test_guide_documents_the_current_schedule(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn("周二至周日", guide)
        self.assertIn("周一", guide)
        self.assertIn("上一完整自然周", guide)
        self.assertIn("普通幂等重试", guide)

    def test_guide_covers_required_configuration(self):
        guide = GUIDE.read_text(encoding="utf-8")
        for variable in (
            "AI_ANALYSIS_ENABLED",
            "AI_API_KEY",
            "AI_MODEL",
            "AI_API_BASE",
            "WEWORK_WEBHOOK_URL",
            "DOCKER_PROXY_URL",
            "ELSEVIER_API_KEY",
            "ELSEVIER_INST_TOKEN",
            "CRON_SCHEDULES",
            "RUN_MODE",
            "IMMEDIATE_RUN",
        ):
            with self.subTest(variable=variable):
                self.assertIn(variable, guide)

    def test_guide_preserves_secrets_and_business_state(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn("禁止提交 Git", guide)
        self.assertIn("docker/.env", guide)
        self.assertIn("output/", guide)
        self.assertIn("不要使用 `docker compose down -v`", guide)
        self.assertIn("不要手工删除 SQLite 检查点", guide)

    def test_guide_contains_no_removed_compose_entrypoint(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertNotIn("docker/docker-compose.yml", guide)


if __name__ == "__main__":
    unittest.main()
