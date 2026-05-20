from fastapi import FastAPI
from pydantic import BaseModel
from anthropic import Anthropic
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create FastAPI app
app = FastAPI()

# Initialize Anthropic client
client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)

# Lead model
class Lead(BaseModel):
    lead_name: str
    location: str
    budget_range: str
    purpose: str

# Analyze endpoint
@app.post("/analyze")
def analyze_lead(lead: Lead):

    prompt = f"""
Analyze this real estate lead.

Return ONLY in this exact format:

Classification: [VIP/HOT/NORMAL]
Reason: [short reason]
Recommended Action: [next sales step]

Lead Information:
Name: {lead.lead_name}
Location: {lead.location}
Budget: {lead.budget_range}
Purpose: {lead.purpose}
"""

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    result_text = response.content[0].text

    return {
        "status": "success",
        "analysis": result_text
    }