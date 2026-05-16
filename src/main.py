"""Entry point — orchestrates the full analysis pipeline."""

import logging
import os
import shutil

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
PLACEHOLDER = os.path.join(os.path.dirname(__file__), "dashboard", "placeholder.html")


def main():
    log.info("Portfolio Analyst starting — Phase 1 placeholder run")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    placeholder_src = os.path.join(
        os.path.dirname(__file__), "dashboard", "placeholder.html"
    )
    output_path = os.path.join(OUTPUT_DIR, "index.html")
    shutil.copy(placeholder_src, output_path)
    log.info("Placeholder dashboard written to %s", output_path)


if __name__ == "__main__":
    main()
