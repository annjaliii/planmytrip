from flask import Blueprint, request, jsonify

chatbot_bp = Blueprint('chatbot', __name__)

# =========================
# HELPER: DETECT DESTINATION (IMPROVED)
# =========================
def extract_destination(message):
    known_places = [
        "goa", "manali", "bali", "maldives",
        "pune", "mumbai", "delhi", "shimla", "kashmir"
    ]

    msg = message.lower()

    for place in known_places:
        if place in msg:
            return place.capitalize()

    return None


# =========================
# SMART REPLY FUNCTION
# =========================
def generate_reply(message):

    msg = message.lower()
    destination = extract_destination(message)

    # Greeting
    if "hi" in msg or "hello" in msg:
        return "👋 Hello! I’m your AI Travel Assistant. Ask me about trips, budget, hotels, or destinations!"

    # Budget
    if "budget" in msg:
        return f"💰 For {destination or 'your trip'}, use this formula:\n50% hotel, 30% food, 20% transport."

    # Days
    if "days" in msg:
        return f"📅 Ideal duration for {destination or 'most trips'} is 3–5 days."

    # Hotels
    if "hotel" in msg or "stay" in msg:
        return f"🏨 In {destination or 'this destination'}, you’ll find budget hotels, mid-range options, and luxury resorts."

    # Food
    if "food" in msg or "eat" in msg:
        return f"🍽 {destination or 'this place'} offers delicious local food along with restaurants and cafes."

    # Transport
    if "transport" in msg or "travel" in msg:
        return f"🚗 You can travel using buses, taxis, or rental bikes in {destination or 'this city'}."

    # Places
    if "places" in msg or "visit" in msg or "see" in msg:
        return f"📍 Popular places in {destination or 'this destination'} include tourist attractions, local markets, and scenic spots."

    # Plan
    if "plan" in msg:
        return "🧠 I can generate a full trip plan! Just go to planner page and enter destination, days, and budget."

    # Fallback
    return f"🤖 I understand you're asking about '{message}'. Try asking about hotels, budget, places, or trip planning."


# =========================
# MAIN CHAT ROUTE (FIXED)
# =========================
@chatbot_bp.route("/chat", methods=["POST"])
def chat():
    try:
        # Force JSON parsing (prevents None error)
        data = request.get_json(force=True)

        if not data or "message" not in data:
            return jsonify({"reply": "⚠️ Please send a valid message."})

        user_msg = data["message"].strip()

        if user_msg == "":
            return jsonify({"reply": "⚠️ Message cannot be empty."})

        reply = generate_reply(user_msg)

        return jsonify({"reply": reply})

    except Exception as e:
        print("Chatbot Error:", e)  # Debug in terminal
        return jsonify({"reply": "⚠️ Server error. Please try again."})