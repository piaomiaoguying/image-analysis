"""图片识别与理解管理器"""
import os
import sys
import json
import base64
import requests
from typing import Dict, List, Optional, Tuple
from pathlib import Path


class VisionManager:
    """图片识别管理器"""

    def __init__(self, config: Dict):
        self.config = config
        self.default_model = config.get('default_model', 'zhipu')
        self.providers = config.get('providers', {})

    def _encode_image(self, image_path: str) -> str:
        """将本地图片转换为 base64"""
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

    def _is_local_file(self, path: str) -> bool:
        """判断是否为本地文件"""
        return os.path.exists(path) or (not path.startswith('http://') and not path.startswith('https://'))

    def _prepare_image_url(self, image_path: str) -> str:
        """准备图片 URL：本地文件转 Base64，网络 URL 直接返回"""
        if image_path.startswith('data:'):
            return image_path
        if self._is_local_file(image_path):
            ext = Path(image_path).suffix.lower()
            mime_type = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp'
            }.get(ext, 'image/jpeg')

            base64_image = self._encode_image(image_path)
            return f"data:{mime_type};base64,{base64_image}"
        else:
            return image_path

    def _get_provider(self, name: str) -> Dict:
        provider = self.providers.get(name)
        if not provider:
            raise ValueError(f"不支持的模型: {name}，可用模型: {', '.join(self.providers.keys())}")
        return provider

    def analyze(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
        model: Optional[str] = None,
        stream: bool = False,
        timeout: Optional[Tuple[float, float]] = None
    ) -> Dict:
        """分析图片"""
        provider_name = model or self.default_model
        provider = self._get_provider(provider_name)

        # 构建消息内容
        content = []

        if images:
            for image in images:
                image_url = self._prepare_image_url(image)
                content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })

        content.append({
            "type": "text",
            "text": prompt
        })

        # 构建请求
        payload = {
            "model": provider['model'],
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ]
        }

        # 必须显式声明非流式：new-api 一类网关在既无 stream 字段、又无 Accept 头时
        # 默认返回 text/event-stream，response.json() 会直接解析失败
        payload["stream"] = bool(stream)

        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        if timeout is not None:
            req_timeout = tuple(timeout)
        else:
            req_timeout = provider.get('timeout', self.config.get('timeout', [5, 120]))
            if isinstance(req_timeout, (int, float)):
                req_timeout = (req_timeout, req_timeout)
            req_timeout = tuple(req_timeout)

        response = requests.post(
            provider['base_url'], headers=headers, json=payload, timeout=req_timeout
        )
        response.raise_for_status()

        try:
            result = response.json()
        except ValueError:
            ctype = response.headers.get('content-type', 'unknown')
            raise RuntimeError(f"返回非 JSON（content-type={ctype}）")

        if not self._extract_content(result).strip():
            raise RuntimeError("返回空内容")

        return result

    @staticmethod
    def _extract_content(result: Dict) -> str:
        """取出首条回复的文本，取不到则返回空串"""
        try:
            content = result['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            return ''
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get('text') or '')
                elif isinstance(item, str):
                    parts.append(item)
            return ''.join(parts)
        return ''

    def analyze_with_fallback(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
        stream: bool = False
    ) -> Tuple[Dict, str]:
        """按配置顺序依次尝试 provider，返回第一个成功的结果。

        每次尝试受 provider/config 的 timeout 约束；整条 fallback 链路另有
        总时限（默认 60s）与最大尝试数（默认 12），避免前几个 provider 挂起时
        整次识图看起来「卡死」。
        """
        import time

        deadline = float(self.config.get('fallback_deadline_seconds', 60))
        max_attempts = int(self.config.get('fallback_max_attempts', 12))
        started = time.monotonic()
        errors = []
        tried = 0

        for name in self.providers:
            if tried >= max_attempts:
                print(f"  已达最大尝试数 {max_attempts}，停止 fallback")
                break
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 1:
                print(f"  已用满 {deadline:.0f}s 总时限，停止 fallback")
                break

            base = self.providers[name].get('timeout', self.config.get('timeout', [5, 120]))
            if isinstance(base, (int, float)):
                base = (base, base)
            attempt_timeout = (min(float(base[0]), remaining), min(float(base[1]), remaining))

            tried += 1
            sys.stdout.write(f"  尝试 {name}... ")
            sys.stdout.flush()
            try:
                result = self.analyze(
                    prompt, images, model=name,
                    stream=stream, timeout=attempt_timeout
                )
                print(f"OK ({time.monotonic() - started:.1f}s)")
                return result, name
            except Exception as e:
                msg = str(e)[:80]
                print(f"失败 ({msg})")
                errors.append((name, msg))

        raise RuntimeError(
            f"所有 provider 均失败（已尝试 {tried} 个，耗时 {time.monotonic() - started:.1f}s）:\n"
            + "\n".join(f"  {n}: {e}" for n, e in errors)
        )

    def format_result(self, result: Dict, show_usage: bool = False, model_name: str = None) -> str:
        """格式化输出结果"""
        try:
            content = self._extract_content(result)
            prefix = f"[{model_name}] " if model_name else ""
            output = f"{prefix}分析结果：\n{content}"

            if show_usage and 'usage' in result:
                usage = result['usage']
                output += f"\n\nToken 使用：输入 {usage.get('prompt_tokens', 0)} | 输出 {usage.get('completion_tokens', 0)} | 总计 {usage.get('total_tokens', 0)}"

            return output
        except (KeyError, IndexError) as e:
            return f"解析结果失败: {str(e)}\n原始结果: {json.dumps(result, ensure_ascii=False, indent=2)}"


def load_config(config_path: str = None) -> Dict:
    """加载配置文件"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config.json')

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)
