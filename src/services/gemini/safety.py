"""Shared Gemini safety settings — applied project-wide to all ChatGoogleGenerativeAI instances.

BLOCK_ONLY_HIGH: block only when Gemini rates content as HIGH severity.
Children's-book content (characters, illustrations, narration) triggers false
positives at the default BLOCK_MEDIUM_AND_ABOVE threshold; this setting reduces
those while still blocking genuinely harmful output.
"""

from langchain_google_genai import HarmBlockThreshold, HarmCategory

GEMINI_SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
}
