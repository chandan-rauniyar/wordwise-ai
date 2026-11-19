
import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://127.0.0.1:27017/",
)
DATABASE_NAME = os.getenv("DATABASE_NAME", "wordwise_ai")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[DATABASE_NAME]


def check_connection():
    client.admin.command("ping")
    return True
