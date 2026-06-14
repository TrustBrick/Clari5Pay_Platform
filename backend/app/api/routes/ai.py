from fastapi import APIRouter, Depends, HTTPException
import anthropic
from app.core.config import settings
from app.core.deps import get_current_user
from app.models.models import User
from app.schemas.schemas import AIChatRequest, AIChatResponse

router = APIRouter(prefix="/api/ai", tags=["ai"])

SYSTEM_PROMPT = """You are an intelligent AI assistant for Clari5Pay, an enterprise Payment Service Provider (PSP) platform.

You help users — merchants, admins, and super admins — understand:
- Their transaction history, statuses, and volumes
- Deposit types: UPI, QR, IMPS, NEFT, RTGS, CASH
- The 2-step approval workflow: Merchant submits → Admin approves → Super Admin completes
- Risk analysis, risk scores, and fraud prevention
- Fee structures: pay-in fees, pay-out fees, settlement codes
- Balance enquiries, settlements, and withdrawals
- Platform features: integrations (WhatsApp, Telegram, Email), profile management

Always be helpful, precise, and professional. When discussing amounts, use Indian Rupee (₹) formatting.
Keep responses concise and actionable. If asked about specific transaction data you don't have access to, 
acknowledge that and guide the user to check the relevant section of the platform.

Platform roles:
- MERCHANT: Can submit deposits, withdrawals, settlements; view own transactions
- ADMIN: Can approve/reject pending transactions; manage merchants
- SUPER_ADMIN: Final approvals; manages admins and merchants; risk intelligence
"""


@router.post("", response_model=AIChatResponse)
async def ai_chat(
    request: AIChatRequest,
    current_user: User = Depends(get_current_user),
):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI service not configured")

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    messages = [
        {"role": msg.role, "content": msg.content}
        for msg in request.messages
        if msg.role in ("user", "assistant")
    ]

    # Inject user context into first message if needed
    user_context = f"\n\n[User context: {current_user.name}, Role: {current_user.role}, Balance: ₹{current_user.balance or 0:,.2f}]"
    if messages and messages[0]["role"] == "user":
        messages[0] = {
            "role": "user",
            "content": messages[0]["content"] + user_context,
        }

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    reply = response.content[0].text
    return AIChatResponse(reply=reply)
