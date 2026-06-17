import os
import sys
from typing import Any, Dict

# Make sure the current directory is in the path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from ssr.config import load_settings
from evalscope import TaskConfig, run_task
from evalscope.agent.external import ExternalAgentConfig
from evalscope.api.registry import register_runner
from evalscope.agent.external.runners.base import AgentRunner, AgentRunResult, BridgeEndpoint, ExternalAgentTask, RunnerTimeoutError
from evalscope.utils.logger import get_logger

logger = get_logger()

@register_runner('ssr-agent')
class SSRAgentRunner(AgentRunner):
    framework: str = 'ssr-agent'

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def setup(self, env: Any) -> None:
        logger.info("SSRAgentRunner setup complete.")

    async def run(
        self,
        task: ExternalAgentTask,
        env: Any,
        bridge: BridgeEndpoint,
    ) -> AgentRunResult:
        env_vars: Dict[str, str] = {
            'OPENAI_BASE_URL': f'{bridge.base_url}/openai/v1',
            'OPENAI_API_KEY': bridge.trial_token,
            'GEMINI_API_KEY': bridge.trial_token,
            'SSR_USE_OPENAI': '1',
            'TAVILY_API_KEY': os.environ.get('TAVILY_API_KEY', ''),
        }
        cmd = [sys.executable, '-m', 'ssr', 'ask', task.instruction]
        logger.info(f"SSRAgentRunner launching: {cmd} with bridge {bridge.base_url}")
        
        result = await env.exec(
            cmd,
            timeout=task.timeout,
            env=env_vars,
        )
        logger.info(f"SSRAgentRunner exited with code {result.returncode}")
        
        if result.timed_out:
            raise RunnerTimeoutError(f"ssr-agent timed out after {task.timeout}s")
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"ssr-agent exited with code {result.returncode}: {stderr}")
            
        return AgentRunResult(
            output=result.stdout.strip(),
            metrics={
                'wall_time': result.duration,
                'returncode': result.returncode,
            }
        )

def main():
    settings = load_settings()
    api_key = settings.gemini_api_key
    if not api_key:
        print("Error: GEMINI_API_KEY not found in ~/.ssr/.env")
        sys.exit(1)
        
    task_config = TaskConfig(
        model=settings.default_model,
        api_url='https://generativelanguage.googleapis.com/v1beta/openai/',
        api_key=api_key,
        eval_type='openai_api',
        datasets=['swe_bench_verified'],
        limit=1,  # Run 1 sample to quickly verify the custom runner and benchmark
        agent_config=ExternalAgentConfig(
            framework='ssr-agent',
            environment='local',
            timeout=1800.0,
        ),
    )
    
    print(f"Starting evalscope benchmark for ssr-agent using model {settings.default_model}...")
    run_task(task_config)
    print("Benchmark complete!")

if __name__ == '__main__':
    main()
