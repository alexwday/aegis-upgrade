"""
Setup configuration for Aegis Agent.
"""

from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="aegis-agent",
    version="0.1.0",
    author="Alex Day",
    description="Transcripts-first conversational financial research agent",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.11",
    install_requires=requirements,
    include_package_data=True,
    package_data={
        "aegis_agent": [
            "model/prompts/**/*.yaml",
            "utils/ssl/*.cer",
        ],
    },
)
