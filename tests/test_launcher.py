"""Unit tests for ForgeCode interactive launcher."""

from __future__ import annotations

from pathlib import Path

from coding_agent.launcher import c, write_toml_config


def test_color_wrap() -> None:
    """Test that c() returns correct color code wrap if color is enabled."""
    wrapped = c("32", "hello")
    assert "hello" in wrapped


def test_write_toml_config(tmp_path: Path) -> None:
    """Test writing TOML configuration atomically."""
    config_file = tmp_path / "config.toml"
    write_toml_config(
        config_file,
        provider="openai",
        model_name="deepseek-chat",
        api_key="sk-testkey",
        base_url="https://api.deepseek.com/v1",
    )
    
    assert config_file.is_file()
    content = config_file.read_text(encoding="utf-8")
    assert "[model]" in content
    assert 'provider = "openai"' in content
    assert 'name = "deepseek-chat"' in content
    assert 'api_key = "sk-testkey"' in content
    assert 'base_url = "https://api.deepseek.com/v1"' in content


def test_save_cli_config_permanently(tmp_path: Path) -> None:
    """Test merging and saving CLI arguments into .forgecode.toml permanently."""
    from coding_agent.launcher import save_cli_config_permanently
    
    # 1. Save initial config
    save_cli_config_permanently(
        tmp_path,
        model="deepseek-chat",
        provider="openai",
        api_key="sk-initial",
    )
    
    config_file = tmp_path / ".forgecode.toml"
    assert config_file.is_file()
    content = config_file.read_text(encoding="utf-8")
    assert 'name = "deepseek-chat"' in content
    assert 'api_key = "sk-initial"' in content

    # 2. Save new model without wiping existing API key
    save_cli_config_permanently(
        tmp_path,
        model="gpt-4o",
    )
    
    content = config_file.read_text(encoding="utf-8")
    assert 'name = "gpt-4o"' in content
    assert 'api_key = "sk-initial"' in content  # Kept!

