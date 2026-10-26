"""Executor integral local. Não contém HTTP, SDK ou credenciais."""

from cripto_bot.domain.portfolio.models import Plan


class PaperExecutor:
    def fill(self, plan: Plan) -> dict[str, object]:
        return {"action_key": plan.action, "fill_id": plan.fill_id}
