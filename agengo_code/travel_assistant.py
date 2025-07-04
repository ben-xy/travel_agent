import operator
from pydantic import BaseModel, Field
from typing import Annotated, List, Dict, Any, Optional
from typing_extensions import TypedDict
import requests
import json
import os, getpass
from dotenv import load_dotenv

# LangChain and LangGraph imports for LLM, tools, and workflow
from langchain_community.document_loaders import WikipediaLoader
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, get_buffer_string
from langchain_openai import ChatOpenAI

from langgraph.types import Send
from langgraph.graph import END, MessagesState, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

# Helper function to set environment variables interactively if not set
def _set_env(var: str):
    if not os.environ.get(var):
        os.environ[var] = getpass.getpass(f"{var}: ")

# Load environment variables from .env file and prompt for missing keys
load_dotenv()
_set_env("OPENAI_API_KEY")
_set_env("OPENWEATHER_KEY")
_set_env("TAVILY_API_KEY")

# Get environment variables for API keys
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENWEATHER_KEY = os.environ.get("OPENWEATHER_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

### LLM initialization

llm = ChatOpenAI(model="gpt-4o", temperature=0)

### Data Schemas

class Traveler(BaseModel):
    """Represents a traveler persona."""
    name: str = Field(description="Name of the traveler.")
    description: str = Field(description="Description of the traveler's interests, concerns.")

    @property
    def persona(self) -> str:
        return f"Name: {self.name}\nDescription: {self.description}\n"

class Perspectives(BaseModel):
    """List of traveler personas."""
    travelers: List[Traveler] = Field(
        description="Comprehensive list of travelers with their interests and concerns.",
    )

class GenerateTravelersState(TypedDict):
    """State for traveler generation."""
    city: str
    weather: List[Dict[str, Any]]
    days: int
    max_travelers: int
    human_feedback_traveler: str
    travelers: List[Traveler]

class dialogueState(MessagesState):
    """State for dialogue between traveler and local."""
    max_num_turns: int  # Number of conversation turns
    context: Annotated[list, operator.add]
    traveler: Traveler  # Traveler persona
    dialogue: str  # Dialogue transcript
    sections: Annotated[list, operator.add]  # For Send() API
    city: str

class dialogueOutputState(MessagesState):
    """Output state for dialogue."""
    context: Annotated[list, operator.add]
    traveler: Traveler
    dialogue: str
    sections: Annotated[list, operator.add]

class SearchQuery(BaseModel):
    """Schema for search query."""
    search_query: str = Field(None, description="Search query for retrieval.")

class TravelGraphState(TypedDict):
    """Main workflow state."""
    city: str
    weather: List[Dict[str, Any]]
    days: int
    max_travelers: int
    human_feedback_traveler: str
    human_feedback_plan: str
    travelers: List[Traveler]
    sections: Annotated[list, operator.add]
    content: str
    final_plan: str

### Nodes and workflow logic

# Instructions for traveler persona generation
traveler_instructions = """You are tasked with creating a set of AI traveler personas. Follow these instructions carefully:

1. First, review the travel city:
{city}

2. Check the weather for the city:
{weather}

3. Check the number of days for the trip:
{days}
        
4. Examine any editorial feedback that has been optionally provided to guide creation of the travelers: 
        
{human_feedback_traveler}
    
5. Determine the most interesting travel topics. If there is feedback above, you should consider it.
                    
6. Pick the top {max_travelers} topics.

7. Assign one traveler to each topic."""

def get_latlon(city: str):
    """Get latitude and longitude for a destination using OpenStreetMap."""
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": city, "format": "json", "limit": 1},
        headers={"User-Agent": "Travel-Agent"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Cannot resolve coordinates for '{city}'")
    return f"{data[0]['lat']},{data[0]['lon']}"

def get_weather(city: str, days: int = 5) -> List[Dict]:
    """Get weather information for a city using OpenWeather API."""
    if not OPENWEATHER_KEY:
        return [{"error": "OpenWeather API key not set"}]

    try:
        lat, lon = map(float, get_latlon(city).split(","))
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={
                "lat": lat,
                "lon": lon,
                "appid": OPENWEATHER_KEY,
                "units": "metric",
                "lang": "en",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()["list"]

        # Aggregate weather data by day
        daily = {}
        for item in data:
            date = item["dt_txt"][:10]
            if date not in daily:
                daily[date] = {"temps": [], "descs": [], "pops": []}
            daily[date]["temps"].append(item["main"]["temp"])
            daily[date]["descs"].append(item["weather"][0]["description"])
            daily[date]["pops"].append(item.get("pop", 0.0))

        out = []
        for date, rec in sorted(daily.items())[:days]:
            out.append({
                "date": date,
                "summary": max(set(rec["descs"]), key=rec["descs"].count),
                "temp_max": round(max(rec["temps"]), 1),
                "temp_min": round(min(rec["temps"]), 1),
                "pop_max": round(max(rec["pops"]), 2),
            })
        # Print weather summary for debugging
        for day in out:
            if isinstance(day, dict):
                date = day.get('date', 'N/A')
                summary = day.get('summary', 'N/A')
                temp_min = day.get('temp_min', 'N/A')
                temp_max = day.get('temp_max', 'N/A')
                pop = day.get('pop_max', 'N/A')
                print(f"📅 {date}: {temp_min}°C - {temp_max}°C | {summary} | Rain Probability: {pop}")
        return out
    except Exception as e:
        return [{"error": f"Failed to get weather information: {str(e)}"}]

def get_weather_info(state: TravelGraphState):
    """Node: Get weather information for the selected city."""
    city = state.get("city", 'tokyo')
    days = state.get("days", 5)
    try:
        weather = get_weather(city, days)
    except Exception as e:
        weather = [{"error": f"Failed to get weather: {str(e)}"}]
    return {
        "weather": weather
    }

def create_travelers(state: TravelGraphState):
    """Node: Create traveler personas based on city, weather, days, and feedback."""
    city = state['city']
    weather = state['weather']
    days = state['days']
    max_travelers = state['max_travelers']
    human_feedback_traveler = state.get('human_feedback_traveler', '')
    # Use LLM with structured output for traveler generation
    structured_llm = llm.with_structured_output(Perspectives)
    system_message = traveler_instructions.format(
        city=city,
        weather=weather,
        days=days,
        human_feedback_traveler=human_feedback_traveler,
        max_travelers=max_travelers
    )
    travelers = structured_llm.invoke(
        [SystemMessage(content=system_message)] +
        [HumanMessage(content="Generate the set of travelers.")]
    )
    return {"travelers": travelers.travelers}

def feedback_traveler(state: TravelGraphState):
    """No-op node for traveler feedback interruption."""
    pass

# Instructions for traveler-local dialogue
question_instructions = """You are an traveler tasked with talking to a local who has been living in your destination city for over 20 years to get advice about your trip. 

Your goal is to boil down to interesting and specific information related to your trip.

1. Interesting: Insights that people will find surprising or non-obvious.
        
2. Specific: Insights that avoid generalities and include specific examples from the local.

Here is your interest topic: {topic}

Begin by introducing yourself using a name that fits your persona, and then ask your question.

Continue to ask questions to drill down and refine your expectations of the trip.
        
When you are satisfied with your goals, complete the talk with: "Thank you so much for your help!"

Remember to stay in character throughout your response, reflecting the persona and goals provided to you."""

def generate_question(state: dialogueState):
    """Node: Generate a question from the traveler to the local."""
    traveler = state["traveler"]
    messages = state["messages"]
    system_message = question_instructions.format(topic=traveler.persona)
    question = llm.invoke([SystemMessage(content=system_message)] + messages)
    return {"messages": [question]}

# Instructions for search query generation
search_instructions = SystemMessage(content="""You will be given a conversation between a traveler and a local. 

Your goal is to generate a well-structured query for use in retrieval and / or web-search related to the conversation.
        
First, analyze the full conversation.

Pay particular attention to the final question posed by the traveler.

Convert this final question into a well-structured web search query""")

def search_web(state: dialogueState):
    """Node: Retrieve documents from web search using Tavily."""
    tavily_search = TavilySearchResults(max_results=3)
    structured_llm = llm.with_structured_output(SearchQuery)
    search_query = structured_llm.invoke([search_instructions] + state['messages'])
    search_docs = tavily_search.invoke(search_query.search_query)
    formatted_search_docs = "\n\n---\n\n".join(
        [
            f'<Document href="{doc["url"]}"/>\n{doc["content"]}\n</Document>'
            for doc in search_docs
        ]
    )
    return {"context": [formatted_search_docs]}

def search_wikipedia(state: dialogueState):
    """Node: Retrieve documents from Wikipedia."""
    structured_llm = llm.with_structured_output(SearchQuery)
    search_query = structured_llm.invoke([search_instructions] + state['messages'])
    search_docs = WikipediaLoader(query=search_query.search_query, load_max_docs=2).load()
    formatted_search_docs = "\n\n---\n\n".join(
        [
            f'<Document source="{doc.metadata["source"]}" page="{doc.metadata.get("page", "")}"/>\n{doc.page_content}\n</Document>'
            for doc in search_docs
        ]
    )
    return {"context": [formatted_search_docs]}

# Instructions for local's answer
answer_instructions = """You are a local who has been living in the {city} for over 20 years being taking to a traveler.

Here is the traveler's interest topic: {topic}. 
        
You goal is to answer a question posed by the traveler.

To answer question, use this context:
        
{context}

When answering questions, follow these guidelines:
        
1. Use only the information provided in the context. 
        
2. Do not introduce external information or make assumptions beyond what is explicitly stated in the context.

3. The context contain sources at the topic of each individual document.

4. Include these sources your answer next to any relevant statements. For example, for source # 1 use [1]. 

5. List your sources in order at the bottom of your answer. [1] Source 1, [2] Source 2, etc
        
 """

def generate_answer(state: dialogueState):
    """Node: Generate an answer from the local to the traveler."""
    traveler = state["traveler"]
    messages = state["messages"]
    context = state["context"]
    city = state["city"]
    system_message = answer_instructions.format(city=city, topic=traveler.persona, context=context)
    answer = llm.invoke([SystemMessage(content=system_message)] + messages)
    answer.name = "local"
    return {"messages": [answer]}

def save_dialogue(state: dialogueState):
    """Node: Save the dialogue transcript."""
    messages = state["messages"]
    dialogue = get_buffer_string(messages)
    print("💬💬dialogue:" + state["traveler"].name)
    print("💬" * 50)
    print(dialogue)
    print("💬" * 50)
    return {"dialogue": dialogue}

def route_messages(state: dialogueState, name: str = "local"):
    """Node: Route between question and answer, or finish dialogue."""
    messages = state["messages"]
    max_num_turns = state.get('max_num_turns', 2)
    num_responses = len(
        [m for m in messages if isinstance(m, AIMessage) and m.name == name]
    )
    if num_responses >= max_num_turns:
        return 'save_dialogue'
    last_question = messages[-2]
    if "Thank you so much for your help" in last_question.content:
        return 'save_dialogue'
    return "ask_question"

# Instructions for writing a report section
section_writer_instructions = """You are an expert report writer. 
            
Your task is to create a short, easily digestible section of a plan based on a set of source documents.

1. Analyze the content of the source documents: 
- The name of each source document is at the start of the document, with the <Document tag.
        
2. Write the report following this structure:

a. Summary (### header)
b. Sources (### header)

3. Make your title engaging based upon the topic of the traveler's trip: 
{topic}

4. For the summary section:
- Set up summary with general context related to the topic of the traveler's trip :{topic}
- Emphasize what is novel, interesting, or surprising  gathered from the dialogue:{dialogue}
- Create a numbered list of source documents, as you use them
- Do not mention the names of travelers or locals
- Aim for approximately 200 words maximum
- Use numbered sources in your report (e.g., [1], [2]) based on information from source documents
        
5. In the Sources section:
- Include all sources used in your report
- Provide full links to relevant websites 
- Separate each source by a newline. Use two  spaces at the end of each line to create a newline in Markdown.
- It will look like:

### Sources
[1] Link 
[2] Link 

6. Be sure to combine sources. For example this is not correct:

[3] https://ai.meta.com/blog/meta-llama-3-1/
[4] https://ai.meta.com/blog/meta-llama-3-1/

There should be no redundant sources. It should simply be:

[3] https://ai.meta.com/blog/meta-llama-3-1/
        
7. Final review:
- Ensure the report follows the required structure
- Include no preamble before the title of the report
- Check that all guidelines have been followed"""

def write_section(state: dialogueState):
    """Node: Write a summary section based on the dialogue and context."""
    dialogue = state["dialogue"]
    context = state["context"]
    traveler = state["traveler"]
    system_message = section_writer_instructions.format(topic=traveler.persona, dialogue=dialogue)
    section = llm.invoke(
        [SystemMessage(content=system_message)] +
        [HumanMessage(content=f"Use this source to write your section: {context}")]
    )
    print('summary')
    print("📝" * 50)
    print(section.content)
    print('📝' * 50)
    return {"sections": [section.content]}

# Build the dialogue subgraph
dialogue_builder = StateGraph(dialogueState, output=dialogueOutputState)
dialogue_builder.add_node("ask_question", generate_question)
dialogue_builder.add_node("search_web", search_web)
dialogue_builder.add_node("search_wikipedia", search_wikipedia)
dialogue_builder.add_node("answer_question", generate_answer)
dialogue_builder.add_node("save_dialogue", save_dialogue)
dialogue_builder.add_node("write_section", write_section)

# Dialogue flow
dialogue_builder.add_edge(START, "ask_question")
dialogue_builder.add_edge("ask_question", "search_web")
dialogue_builder.add_edge("ask_question", "search_wikipedia")
dialogue_builder.add_edge("search_web", "answer_question")
dialogue_builder.add_edge("search_wikipedia", "answer_question")
dialogue_builder.add_conditional_edges("answer_question", route_messages, ['ask_question', 'save_dialogue'])
dialogue_builder.add_edge("save_dialogue", "write_section")
dialogue_builder.add_edge("write_section", END)

def conduct_dialogue_router(state: TravelGraphState):
    """Router: For each traveler, start a dialogue subgraph."""
    feedbacks = state.get('human_feedback_traveler')
    print("human_feedback_traveler =", feedbacks)
    if feedbacks:
        return "create_travelers"
    city = state.get("city", "")
    max_travelers = state.get("max_travelers", 3)
    travelers = state.get("travelers", [])
    return [Send("conduct_dialogue_sub", {
        "traveler": traveler,
        "messages": [HumanMessage(content=f"So you said you plan to have a trip on {city}?")],
        "max_num_turns": 2,
        "context": [],
        "dialogue": "",
        "sections": [],
        "city": city,
        "max_travelers": max_travelers
    }) for traveler in travelers]

# Instructions for writing the final travel plan
plan_writer_instructions = """You are a professional travel planner creating a travel plan on this  city: {city}
    
You got information from a team of travelers. Each traveler has done two things: 

1. They conducted a dialogue with a local on a specific goal of the trip.
2. They write up their finding into a memo.

Your task: 

1. You will be given a collection of memos from travelers.

2. Consolidate these into a comprehensive {days} day travel plan that ties together the information from all of the memos. 

5. IMPORTANT: Please consider the weather conditions when planning activities: {weather}
    - For rainy days: Plan indoor activities (museums, shopping malls, restaurants, indoor attractions)
    - For sunny days: Plan outdoor activities (parks, outdoor attractions, walking tours)
    - For hot weather: Include air-conditioned venues and suggest appropriate clothing
    - For cold weather: Include indoor activities and suggest warm clothing
    - Adjust transportation plans based on weather (avoid walking in heavy rain)
    
    Please generate a structured travel plan including:
    1. Trip overview with weather considerations
    2. Daily detailed itinerary that adapts to weather conditions - IMPORTANT: For each day, explicitly mention the weather and how it affects the activities chosen
    3. Budget estimation
    
    IMPORTANT: In the budget section, for each item and the total, show both SGD and the local currency of the destination (e.g., KRW for Seoul, JPY for Tokyo, EUR for Paris). Use a reasonable approximate exchange rate. e.g., 1 SGD ≈ 1,064 KRW, 1 SGD ≈ 113 JPY, 1 SGD ≈ 0.67 EUR). Format: $100 / ₩106400.
    
    4. Important notes and tips including weather-appropriate clothing and activities
    
    Format should be clear and easy to read. For each day in the itinerary, start with the weather information and explain why specific activities were chosen based on the weather conditions.
To format your travel plan:
 

6. Do not mention any traveler or local names in your travel plan.

7. IMPORTANT: if the human feedback is not empty, you should consider it and incorporate it into your travel plan: {human_feedback_plan}

8. IMPORTANT: In this travel plan:
- Include all sources used 
- Provide full links to relevant websites 
- Separate each source by a newline. Use two  spaces at the end of each line to create a newline in Markdown.
- It will look like:

### Sources
[1] Link 
[2] Link 

6. Be sure to combine sources. For example this is not correct:

[3] https://ai.meta.com/blog/meta-llama-3-1/
[4] https://ai.meta.com/blog/meta-llama-3-1/

There should be no redundant sources. It should simply be:

[3] https://ai.meta.com/blog/meta-llama-3-1/

Here are the memos from your travelers to build your travel plan from: 

{context}"""

def write_plan(state: TravelGraphState):
    """Node: Write the final travel plan based on all traveler memos and weather."""
    days = state["days"]
    sections = state["sections"]
    city = state["city"]
    weather = state["weather"]
    human_feedback_plan = state["human_feedback_plan"]
    formatted_str_sections = "\n\n".join([f"{section}" for section in sections])
    system_message = plan_writer_instructions.format(
        city=city,
        days=days,
        weather=weather,
        context=formatted_str_sections,
        human_feedback_plan=human_feedback_plan
    )
    plan = llm.invoke([SystemMessage(content=system_message)] + [HumanMessage(content=f"Write a travel plan based upon these memos.")])
    return {"final_plan": plan.content}

def feedback_plan(state: TravelGraphState):
    """No-op node for plan feedback interruption."""
    pass

def initiate_all_plans(state: TravelGraphState):
    """Node: Decide whether to write plan or end based on feedback."""
    feedbacks = state.get('human_feedback_plan')
    if not feedbacks:
        return END
    else:
        return "write_plan"

# Build the main workflow graph
builder = StateGraph(TravelGraphState)
builder.add_node("get_weather_info", get_weather_info)
builder.add_node("create_travelers", create_travelers)
builder.add_node("human_feedback_traveler_node", feedback_traveler)
builder.add_node("conduct_dialogue_router", conduct_dialogue_router)
builder.add_node("conduct_dialogue_sub", dialogue_builder.compile())
builder.add_node("write_plan", write_plan)

# Main workflow logic
builder.add_edge(START, "get_weather_info")
builder.add_edge("get_weather_info", "create_travelers")
builder.add_edge("create_travelers", "human_feedback_traveler_node")
builder.add_conditional_edges("human_feedback_traveler_node", conduct_dialogue_router, ["create_travelers", "conduct_dialogue_sub"])
builder.add_edge("conduct_dialogue_sub", "write_plan")
builder.add_edge("write_plan", END)

# Compile the workflow graph with memory checkpointing
memory = MemorySaver()
graph = builder.compile(interrupt_before=['human_feedback_traveler_node'], checkpointer=memory)
