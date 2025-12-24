from langchain_openai import AzureChatOpenAI
from dotenv import load_dotenv
import os

# Load the Environment Variables:
load_dotenv()

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION")

os.environ["AZURE_OPENAI_ENDPOINT"] = AZURE_OPENAI_ENDPOINT


llm = AzureChatOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_API_VERSION,
    azure_deployment="gpt-4o"
)