"""Diagnose LLM connectivity step by step:  python -m cli.llm_check"""
from engine.llm_check import diagnose, summary

if __name__ == "__main__":
    print(summary(diagnose()))
