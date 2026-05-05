from abc import ABC, abstractmethod


class Tool(ABC):
    """工具基类。子类只需定义 name/description/parameters 和 execute 方法。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，LLM 通过它来调用工具"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，告诉 LLM 这个工具的用途"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """工具参数的 JSON Schema"""
        ...

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """执行工具，返回字符串结果"""
        ...

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
