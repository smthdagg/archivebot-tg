"""三行原文摘要测试。"""

from app.archive.excerpt import extract_excerpt


def test_three_sentences():
    text = (
        "人工智能正在快速改变软件开发流程。"
        "从代码生成到自动测试，开发者的工作方式正在发生变化。"
        "未来的软件工程可能更多转向需求、架构与验证。"
        "这是第四句，不应出现在前三行中。"
    )
    result = extract_excerpt(text)
    lines = result.split("\n")
    assert len(lines) == 3
    assert "人工智能正在快速改变" in lines[0]
    assert "架构与验证" in lines[2]
    assert "第四句" not in result


def test_filters_noise():
    text = (
        "  标题噪音 https://example.com  https://x.com/a  #话题\n"
        "正文第一句内容非常充实完整。"
        "正文第二句内容也很充实完整。"
        "正文第三句内容同样充实完整。"
    )
    result = extract_excerpt(text)
    assert "http" not in result
    assert "正文第一句" in result


def test_empty():
    assert extract_excerpt("") == ""
    assert extract_excerpt("   ") == ""
