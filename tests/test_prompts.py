from file_agent.prompts import SYSTEM_INSTRUCTIONS


def test_system_instructions_define_final_answer_and_source_date_rules() -> None:
    assert "不要生成面向用户的回答 message 或进度文字" in SYSTEM_INSTRUCTIONS
    assert "允许生成 reasoning summary 和 function call" in SYSTEM_INSTRUCTIONS
    assert "文档元数据或正文" in SYSTEM_INSTRUCTIONS
    assert "Date、日志时间戳" in SYSTEM_INSTRUCTIONS
    assert "没有前述日期时的文件名日期" in SYSTEM_INSTRUCTIONS
    assert "业务事实日期不能替代源文件自身的记录日期" in SYSTEM_INSTRUCTIONS
