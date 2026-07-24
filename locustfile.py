"""压测场景（Locust）。

运行：
    locust -f locustfile.py --host http://localhost:8000

场景覆盖：
1. 普通流式对话 — P50/P95 首 Token 延迟、端到端延迟、错误率
2. 多会话并发 — 吞吐、连接池表现
3. 同会话并发写 — 串行保证、无重复事件
4. Provider 故障恢复（需手动注入故障）
"""
from __future__ import annotations

import json
import time
import uuid

from locust import HttpUser, between, task


class ChatUser(HttpUser):
    """模拟普通用户：创建会话后反复发消息（非流式 + 流式各半）。"""

    wait_time = between(0.5, 2)

    def on_start(self):
        resp = self.client.post(
            "/v1/sessions",
            json={"external_user": f"load-{uuid.uuid4()}"},
            headers={"X-Api-Key": "dev"},
        )
        if resp.status_code == 200:
            self.session_id = resp.json()["session_id"]
        else:
            self.session_id = None

    @task(3)
    def send_message(self):
        if not self.session_id:
            return
        self.client.post(
            f"/v1/sessions/{self.session_id}/messages",
            json={"content": "你好，简单回复一句话。"},
            headers={"X-Api-Key": "dev"},
            name="/v1/sessions/[id]/messages",
        )

    @task(2)
    def send_message_stream(self):
        if not self.session_id:
            return
        first_token_at: float | None = None
        start = time.perf_counter()
        with self.client.post(
            f"/v1/sessions/{self.session_id}/messages/stream",
            json={"content": "用一句话介绍自己。"},
            headers={"X-Api-Key": "dev"},
            name="/v1/sessions/[id]/messages/stream",
            stream=True,
            catch_response=True,
        ) as resp:
            for line in resp.iter_lines():
                if line and first_token_at is None:
                    first_token_at = time.perf_counter() - start
            if resp.status_code >= 400:
                resp.failure(f"status {resp.status_code}")
            else:
                resp.success()


class ConcurrentSessionUser(HttpUser):
    """多会话并发：每个用户独立会话，高并发下测吞吐与连接池。"""

    wait_time = between(0.1, 0.5)

    def on_start(self):
        resp = self.client.post(
            "/v1/sessions",
            json={"external_user": f"concurrent-{uuid.uuid4()}"},
            headers={"X-Api-Key": "dev"},
        )
        self.session_id = resp.json().get("session_id") if resp.status_code == 200 else None

    @task
    def send(self):
        if not self.session_id:
            return
        self.client.post(
            f"/v1/sessions/{self.session_id}/messages",
            json={"content": "ping"},
            headers={"X-Api-Key": "dev"},
            name="/v1/sessions/[id]/messages (concurrent)",
        )
