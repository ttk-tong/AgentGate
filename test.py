import asyncio
from app.domain.llm import ToolCall
from app.domain.tool import ToolContext
from app.orchestration.tool_executor import execute_batched, partition_tool_calls
from app.orchestration.tools import build_default_registry, attach_spawn_agent

async def main():
      reg = build_default_registry()
      print("before:", reg.names())

      class StubRunner:
          async def run(self, spec):
              return f"result:{spec.task}"

      attach_spawn_agent(reg, runner=StubRunner())
      print("after: ", reg.names())

      calls = [
          ToolCall(id="c1", name="spawn_agent", arguments={"task": "查一下巴黎天气"}),
          ToolCall(id="c2", name="spawn_agent", arguments={"task": "查一下东京天气"}),
      ]
      batches = partition_tool_calls(calls, reg)
      print("batches:", len(batches), "concurrent:", batches[0].concurrency_safe)

      results = await execute_batched(calls, reg, ToolContext())
      print([r.content["result"] for r in results])
asyncio.run(main())