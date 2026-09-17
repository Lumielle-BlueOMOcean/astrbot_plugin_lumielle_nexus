"""Small mobile-first aiohttp surface for anonymous Nexus polls."""

from __future__ import annotations

import html
import secrets
from typing import Any

from aiohttp import web

try:
    from .poll_service import PollClosedError, PollError, PollService
except ImportError:
    from poll_service import PollClosedError, PollError, PollService


VOTER_COOKIE = "lumielle_poll_voter"


class PollWeb:
    def __init__(self, service: PollService) -> None:
        self.service = service
        self._runner: web.AppRunner | None = None

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/poll/{token}", self._poll_page)
        app.router.add_post("/api/poll/{token}/vote", self._vote)
        app.router.add_get("/api/poll/{token}/result", self._result)
        app.router.add_get("/healthz", self._healthz)
        return app

    async def start(self, host: str, port: int) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.create_app(), access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, str(host), int(port))
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @staticmethod
    def _new_voter_token() -> str:
        return secrets.token_urlsafe(32)

    def _secure_cookie(self) -> bool:
        return self.service.public_base_url.casefold().startswith("https://")

    def _set_cookie_if_needed(
        self, response: web.StreamResponse, request: web.Request, token: str,
    ) -> None:
        if request.cookies.get(VOTER_COOKIE) is None:
            response.set_cookie(
                VOTER_COOKIE,
                token,
                httponly=True,
                samesite="Strict",
                secure=self._secure_cookie(),
                path="/",
            )

    @staticmethod
    def _error_response(exc: Exception) -> web.Response:
        if isinstance(exc, PollClosedError):
            status = 409
            message = str(exc)
        elif isinstance(exc, PollError):
            status = 400
            message = str(exc)
        else:
            status = 500
            message = "投票服务暂时不可用，请稍后重试。"
        return web.json_response({"ok": False, "error": message}, status=status)

    @staticmethod
    def _render_result(result: dict[str, Any]) -> str:
        lines = ["<section class=\"results\"><h3>当前结果</h3><ol>"]
        for option in result["options"]:
            label = html.escape(str(option["label"]))
            lines.append(
                f"<li>{label} — {option['votes']} 票（{option['percentage']:.1f}%）</li>"
            )
        lines.append(f"</ol><p>共 {result['participant_count']} 人参与</p></section>")
        return "".join(lines)

    def _render_page(
        self,
        poll: dict[str, Any],
        ballot: list[int],
        public_result: dict[str, Any],
    ) -> str:
        title = html.escape(str(poll["title"]), quote=True)
        description = html.escape(str(poll.get("description") or ""), quote=True)
        token = html.escape(str(poll["public_token"]), quote=True)
        deadline = html.escape(str(poll.get("deadline_display") or "未设置"), quote=True)
        disabled = poll["status"] != "OPEN" or (
            bool(ballot) and not poll["allow_change"]
        )
        if poll["status"] == "OPEN":
            if ballot and poll["allow_change"]:
                action_text = "修改投票"
                status_text = "你已经投过票，可以修改。"
            elif ballot:
                action_text = "已提交"
                status_text = "投票已提交，当前投票不可修改。"
            else:
                action_text = "提交投票"
                status_text = ""
        elif poll["status"] == "CLOSED":
            action_text = "投票已结束"
            status_text = "投票已结束。"
        else:
            action_text = "投票已取消"
            status_text = "投票已取消。"
        option_controls: list[str] = []
        input_type = "checkbox" if poll["multiple_choice"] else "radio"
        for option in poll["options"]:
            option_id = int(option["option_id"])
            label = html.escape(str(option["label"]), quote=True)
            checked = " checked" if option_id in ballot else ""
            option_controls.append(
                f'<label><input type="{input_type}" name="choices" value="{option_id}"{checked}{" disabled" if disabled else ""}> {label}</label>'
            )
        result_html = ""
        if public_result.get("visible"):
            result_html = self._render_result(public_result["result"])
        elif public_result.get("message"):
            result_html = f"<p>{html.escape(str(public_result['message']))}</p>"
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 微光·群枢</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:680px;margin:0 auto;padding:24px 16px;background:#f6f4ff;color:#252238}}
main{{background:#fff;border-radius:16px;padding:24px;box-shadow:0 8px 30px #31266b18}}h1{{font-size:18px}}h2{{font-size:24px}}p{{line-height:1.6;color:#5e5872}}
label{{display:block;padding:12px 0;font-size:18px}}input{{margin-right:8px;transform:scale(1.2)}}button{{width:100%;padding:12px;border:0;border-radius:10px;background:#6b54d9;color:#fff;font-size:16px}}button:disabled{{background:#aaa}}
</style></head><body><main>
<h1>微光·群枢 / Lumielle Nexus</h1><p>【群投票】</p><h2>{title}</h2>
<p>{description}</p><form id="poll-form"><fieldset style="border:0;padding:0">{''.join(option_controls)}</fieldset>
<p>截止：{deadline}</p><p>{html.escape(status_text)}</p><button type="submit"{" disabled" if disabled else ""}>{html.escape(action_text)}</button></form>
{result_html}
</main><script>
const form=document.getElementById('poll-form');
if(form){{form.addEventListener('submit',async(event)=>{{event.preventDefault();
const choices=Array.from(form.querySelectorAll('input[name="choices"]:checked')).map(item=>Number(item.value));
const response=await fetch('/api/poll/{token}/vote',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{choices}})}});
const data=await response.json(); if(!response.ok){{alert(data.error||'投票失败');return}} window.location.reload();
}})}});
</script></body></html>"""

    async def _poll_page(self, request: web.Request) -> web.Response:
        voter_token = request.cookies.get(VOTER_COOKIE) or self._new_voter_token()
        try:
            poll = await self.service.get_poll_by_token(request.match_info["token"])
            if poll is None:
                return web.Response(text="投票不存在。", status=404, content_type="text/plain")
            ballot = await self.service.get_ballot(poll["public_token"], voter_token)
            public_result = await self.service.get_public_result(poll["public_token"])
            response = web.Response(
                text=self._render_page(poll, ballot, public_result),
                content_type="text/html",
            )
            self._set_cookie_if_needed(response, request, voter_token)
            return response
        except Exception as exc:
            if isinstance(exc, PollError):
                return web.Response(text=str(exc), status=400, content_type="text/plain")
            return web.Response(text="投票页面暂时不可用。", status=500, content_type="text/plain")

    async def _vote(self, request: web.Request) -> web.Response:
        voter_token = request.cookies.get(VOTER_COOKIE) or self._new_voter_token()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise PollError("请求内容必须是 JSON 对象。")
            result = await self.service.vote(
                request.match_info["token"], body.get("choices"), voter_token,
            )
            response = web.json_response({"ok": True, **result})
            self._set_cookie_if_needed(response, request, voter_token)
            return response
        except Exception as exc:
            response = self._error_response(exc)
            self._set_cookie_if_needed(response, request, voter_token)
            return response

    async def _result(self, request: web.Request) -> web.Response:
        try:
            result = await self.service.get_public_result(request.match_info["token"])
            return web.json_response(result)
        except Exception as exc:
            return self._error_response(exc)

    async def _healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})
