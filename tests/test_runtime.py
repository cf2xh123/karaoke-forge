from karaoke_forge.runtime import DemucsRuntime


def test_demucs_runtime_explains_cpu_fallback_on_nvidia() -> None:
    runtime = DemucsRuntime(
        installed=True,
        demucs_version="4.1.0",
        torch_version="2.13.0+cpu",
        device="cpu",
        nvidia_detected=True,
    )

    assert runtime.ready
    assert runtime.short_label_zh == "可用：CPU（可切换 NVIDIA）"
    assert "独立安装脚本" in runtime.detail_zh


def test_demucs_runtime_reports_dedicated_windows_installer() -> None:
    runtime = DemucsRuntime(installed=False, nvidia_detected=False)

    assert not runtime.ready
    assert "安装人声分离（Demucs）.bat" in runtime.detail_zh
