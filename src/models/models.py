# Lazy imports for heavy ML models - only import when needed
from src.core.config import settings

def embedding_model(model_provider: str = "gemini"):
    """
    Get embedding model based on provider
    """
    if model_provider == 'openai':
        from langchain_openai import OpenAIEmbeddings
        # Uses settings.OPENAI_API_KEY
        embedding_model = OpenAIEmbeddings(
            model='text-embedding-3-small', 
            openai_api_key=settings.OPENAI_API_KEY
        )
    elif model_provider == 'gemini':
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        # Uses settings.google_key
        embedding_model = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001", 
            google_api_key=settings.google_key
        )
    else:
        raise ValueError(f"Unsupported model: {model_provider}")
    
    return embedding_model

def call_llm(model_provider: str = "gemini",
             model_name: str = "",
             temperature: float = 1.0,
             top_p: float = 0.95,
             max_tokens = None
    ):
    """
    Get LLM model based on provider
    """
    
    if model_provider == 'openai':
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=model_name,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            api_key=settings.OPENAI_API_KEY
        )
    elif model_provider == 'gemini':
        from langchain_google_genai import ChatGoogleGenerativeAI
        
        if not settings.google_key:
            print("WARNING: GOOGLE_API_KEY and GEMINI_API_KEY are missing from settings!")
        
        llm = ChatGoogleGenerativeAI(
            model=model_name,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            google_api_key=settings.google_key
        )
    else:
        raise ValueError(f"Unsupported model: {model_provider}")
    
    return llm
