#!/usr/bin/env python3
"""
AI Chat Completion Client

This script provides a command-line interface for interacting with Large Language
Models (LLMs) via the Hugging Face Inference API. It allows users to send a single
prompt to a specified model and receive a streamed response.

The script reads your Hugging Face API key from a file (path configurable) and
supports customizing the model, provider, temperature, and other generation
parameters. All settings have sensible defaults so the script works out of the
box when run from its original location.

Usage:
    python ai.py [options]

Examples:
    # Use defaults (reads key from ../resources/key.txt)
    python ai.py

    # Specify a custom prompt and model
    python ai.py --prompt "Explain quantum computing" --model "meta-llama/Llama-3-8B"

    # Use a custom key file location
    python ai.py --key-file /path/to/key.txt

    # Adjust generation parameters
    python ai.py --temperature 0.7 --max-tokens 2048 --top-p 0.9
"""

import argparse
import os
import sys
from pathlib import Path

from huggingface_hub import InferenceClient


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

# Default path: step out of the script folder, go into resources, read key.txt
_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_KEY_FILE = _SCRIPT_DIR.parent / "resources" / "key.txt"

DEFAULT_MODEL = "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct"
DEFAULT_PROVIDER = "nebius"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TOP_P = 0.5
DEFAULT_PROMPT = (
    "Hello, can you tell me an interesting thing about the world we live in?"
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments with sensible defaults."""
    parser = argparse.ArgumentParser(
        description="Send a prompt to a Hugging Face Inference API model.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--key-file",
        type=Path,
        default=_DEFAULT_KEY_FILE,
        help="Path to a file containing the Hugging Face API key on the first line.",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key directly. Overrides --key-file if provided.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help="Hugging Face model ID to use.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=DEFAULT_PROVIDER,
        help="Inference provider (e.g., nebius, together, hf-inference).",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=DEFAULT_PROMPT,
        help="The user message to send to the model.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help="Sampling temperature (0.0 to 2.0).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help="Maximum number of tokens to generate.",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=DEFAULT_TOP_P,
        help="Nucleus sampling top-p value.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming and print the full response at once.",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

def load_api_key(args: argparse.Namespace) -> str:
    """
    Resolve the API key from the command-line argument or key file.

    Raises:
        SystemExit: If no key can be found.
    """
    if args.api_key:
        return args.api_key.strip()

    key_path = args.key_file
    if not key_path.exists():
        print(
            f"Error: Key file does not exist: {key_path}",
            file=sys.stderr,
        )
        print(
            "Provide --api-key or create the key file with your token.",
            file=sys.stderr,
        )
        sys.exit(1)

    key = key_path.read_text().strip()
    if not key:
        print(f"Error: Key file is empty: {key_path}", file=sys.stderr)
        sys.exit(1)

    return key


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    api_key = load_api_key(args)

    # Use the OpenAI-compatible client configuration. The `provider` parameter
    # routes requests through Hugging Face Inference Providers.
    # See: https://huggingface.co/docs/huggingface_hub/guides/inference
    client = InferenceClient(
        provider=args.provider,
        api_key=api_key,
    )

    messages = [
        {"role": "user", "content": args.prompt},
    ]

    # `chat.completions.create` is an OpenAI-compatible alias for
    # `client.chat_completion`. Both work in recent versions of huggingface_hub.
    response = client.chat.completions.create(
        model=args.model,
        messages=messages,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        top_p=args.top_p,
        stream=not args.no_stream,
    )

    if args.no_stream:
        # Non-streaming: response.choices[0].message.content
        print(response.choices[0].message.content)
    else:
        # Streaming: iterate over delta chunks
        for chunk in response:
            # Guard against chunks with empty choices (can happen in edge cases)
            if chunk.choices and chunk.choices[0].delta.content:
                print(chunk.choices[0].delta.content, end="")
        print()  # Final newline


if __name__ == "__main__":
    main()