"""
Interactive command-line chat with the invoicing agent.
Usage:
    python main.py                 # chat
    python main.py --list-models   # show Gemini models available to your API key
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def list_models() -> None:
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for m in client.models.list():
        if "generateContent" in (m.supported_actions or []):
            print(m.name)


def chat() -> None:
    from google.genai.errors import APIError

    from src.agent import InvoiceAgent
    agent = InvoiceAgent()
    print(f"H-BabyCare invoicing agent (model: {agent.model}). Type 'exit' to quit, 'reset' to clear history.\n")
    while True:
        question = input("You: ").strip()
        if question.lower() in {"exit", "quit"}:
            break
        if question.lower() == "reset":
            agent.reset()
            continue
        if not question:
            continue
        try:
            reply = agent.ask(question)
        except APIError as exc:
            print(f"\n[Gemini API error {exc.code}: {exc.message}]\n"
                  "Your question was not answered. Try again shortly, or set a different GEMINI_MODEL in .env.\n")
            continue
        print(f"\nAgent: {reply.text}\n\n{reply.evidence()}\n")


if __name__ == "__main__":
    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
    list_models() if "--list-models" in sys.argv else chat()
