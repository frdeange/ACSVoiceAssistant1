import json
import os
import uuid  # Add uuid import
from dotenv import load_dotenv
from fastapi import WebSocket
from fastapi.websockets import WebSocketState
from openai import AzureOpenAI
import base64
import asyncio
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import DefaultAzureCredential
from azure.communication.sms import SmsClient, SmsSendResult
from rtclient import (
    FunctionCallOutputItem,
    InputAudioBufferAppendMessage,
    InputAudioTranscription,
    InputTextContentPart,
    ItemCreateMessage,
    RTLowLevelClient,
    ResponseCreateMessage,
    ResponseCreateParams,
    ServerMessageType,
    ServerVAD,
    SessionUpdateMessage,
    SessionUpdateParams,
    UserMessageItem,
)

import logging
from aiologger import Logger

from app.recipe_finder import RecipeFinder

logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)
logger = Logger.with_default_handlers()


load_dotenv()

AZURE_OPENAI_REALTIME_ENDPOINT = os.getenv("AZURE_OPENAI_REALTIME_ENDPOINT")
AZURE_OPENAI_REALTIME_SERVICE_KEY = os.getenv("AZURE_OPENAI_REALTIME_SERVICE_KEY")
AZURE_OPENAI_REALTIME_DEPLOYMENT_MODEL_NAME = os.getenv("AZURE_OPENAI_REALTIME_DEPLOYMENT_MODEL_NAME")

ACS_CONNECTION_STRING = os.getenv("ACS_CONNECTION_STRING")
ACS_SMS_CONNECTION_STRING = os.getenv("ACS_SMS_CONNECTION_STRING")

load_dotenv()

class CommunicationHandler:
    voice_name = None or "alloy"
    target_phone_number = os.getenv("TARGET_PHONE_NUMBER")
    system_prompt = (
        None
        or """
        Eres Asistente Canino, una IA experta en encontrar la raza de perro ideal para cada persona.        
        Tu función es:
        - Ayudar a los usuarios a descubrir qué raza de perro se adapta mejor a su estilo de vida y preferencias
        - Guiar la conversación de forma cercana, amable y clara
        - Hacer preguntas clave sobre:
            - Nivel de actividad física y estilo de vida
            - Espacio disponible en casa (piso, casa con jardín, etc.)
            - Experiencia previa con perros
            - Presencia de niños u otras mascotas
            - Tiempo disponible para cuidados y paseos
            - Preferencias sobre tamaño, pelaje o temperamento
        
        Flujo de conversación:
            - Comienza con un saludo breve y acogedor y deja que el usuario te salude
            - Pregunta sobre el estilo de vida y el entorno del usuario
            - Si es necesario, profundiza en aspectos como alergias, tiempo disponible o preferencias estéticas
            - Una vez identificada una raza adecuada, ofrece una breve descripción y explica por qué es una buena opción
        
        Pautas:
            - Mantén las respuestas breves y enfocadas
            - No recomiendes más de 2 razas a la vez para no abrumar
            - Si hay varias opciones viables, ofrece comparar
            - Destaca en 1-2 frases las características clave de la raza sugerida
        """
    )

    def __init__(self, websocket: WebSocket) -> None:
        self.rt_client = None
        self.active_websocket = websocket
        return

    async def start_conversation_async(self) -> None:
        self.rt_client = RTLowLevelClient(
            url=AZURE_OPENAI_REALTIME_ENDPOINT,
            key_credential=AzureKeyCredential(AZURE_OPENAI_REALTIME_SERVICE_KEY),
            azure_deployment=AZURE_OPENAI_REALTIME_DEPLOYMENT_MODEL_NAME,
        )
        try:
            await self.rt_client.connect()
        except Exception as e:
            print(f"Failed to connect to Azure OpenAI Realtime Service: {e}")
            raise e

        functions = [
            {
                "type": "function",
                "name": "get_recipe",
                "description": "Get a recipe based on the cuisine and provided list of ingredients.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cuisine": {
                            "type": "string",
                            "description": "The type of cuisine the user is interested in.",
                        },
                        "ingredients": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "A list of ingredients the user has available.",
                        },
                    },
                    "required": ["cuisine", "ingredients"],
                },
            },
            {
                "type": "function",
                "name": "send_recipe",
                "description": "Send a link to the recipe.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        ]

        session_update_message = {
            "type": "session.update",
            "session": {
                "voice": "alloy",
                "instructions": self.system_prompt,
                "input_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "threshold": 0.6,
                    "silence_duration_ms": 300,
                    "prefix_padding_ms": 200,
                    "type": "server_vad",
                },
                "tools": functions,
            },
        }

        session_update_message_payload = SessionUpdateMessage(**session_update_message)
        await self.rt_client.send(session_update_message_payload)

        # Generate initial call_id that will be used for the entire conversation
        self.conversation_call_id = str(uuid.uuid4())

        content_part = InputTextContentPart(
            text="Estás teniendo una conversación con un usuario que se llama Kiko. Le vas a ayudar a encontrar la mejor raza de perro para él."
        )
        initial_conversation_item = ItemCreateMessage(
            item=UserMessageItem(content=[content_part]),
            call_id=self.conversation_call_id  # Use the stored conversation_call_id
        )

        await self.rt_client.send(message=initial_conversation_item)
        # NOTE: Need to call this to tell OpenAI to start the conversation and say something first to the user
        await self.rt_client.send(ResponseCreateMessage())

        asyncio.create_task(self.receive_messages_async())
        return

    async def send_message_async(self, message: str) -> None:
        try:
            if self.active_websocket.client_state == WebSocketState.CONNECTED:
                await self.active_websocket.send_text(message)
        except Exception as e:
            logger.error(f"Send Message - Failed to send message: {e}")
            raise e

    async def receive_messages_async(self) -> None:
        try:
            while not self.rt_client.closed:
                message: ServerMessageType = await self.rt_client.recv()

                if message is None or self.rt_client.ws.closed:
                    continue
                
                match message.type:
                    case "session.created":
                        print("Session Created Message")
                        print(f"Session Id: {message.session.id}")
                        pass
                    case "error":
                        print(f"Error: {message.error}")
                        pass
                    case "input_audio_buffer.cleared":
                        print("Input Audio Buffer Cleared Message")
                        pass
                    case "input_audio_buffer.speech_started":
                        print(
                            f"Voice activity detection started at {message.audio_start_ms} [ms]"
                        )
                        await self.stop_audio_async()
                        pass
                    case "input_audio_buffer.speech_stopped":
                        pass
                    case "conversation.item.input_audio_transcription.completed":
                        print(f"User:-- {message.transcript}")
                    case "conversation.item.input_audio_transcription.failed":
                        print(f"Error: {message.error}")
                    case "response.done":
                        print("Response Done Message")
                        print(f"  Response Id: {message.response.id}")

                        if message.response.status_details:
                            print(
                                f"Status Details: {message.response.status_details.model_dump_json()}"
                            )
                    case "response.audio_transcript.done":
                        print(f"AI:-- {message.transcript}")
                    case "response.audio.delta":
                        await self.receive_audio(message.delta)
                        pass
                    case "function_call":
                        print(f"Function Call Message: {message}")
                        # Store the original call_id from the function call
                        call_id = message.call_id
                        pass
                    case "response.function_call_arguments.done":
                        print(f"Message: {message}")
                        function_name = message.name
                        args = json.loads(message.arguments)
                        # Use the call_id from the original function call
                        call_id = message.call_id

                        print(f"Function args: {message.arguments}")

                        if function_name == "get_recipe":
                            try:
                                ingredients = args["ingredients"]
                                cuisine = args["cuisine"]
                                recipe_finder_response = await RecipeFinder().find_recipe(cuisine, ingredients)

                                first_recipe_response = next(iter(recipe_finder_response), None)
                                if not first_recipe_response:
                                    await self.rt_client.ws.send_json(
                                        {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "output": "I couldn't find a recipe for you.",
                                                "call_id": call_id  # Use original call_id
                                            }
                                        }
                                    )
                                    continue

                                url = first_recipe_response["url"]
                                recipe_name = first_recipe_response["name"]
                                url_response = f"Here is a recipe for you: {url}"

                                await self.rt_client.ws.send_json(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            # "output": url_response,
                                            "output": f"Here is a recipe for you: {recipe_name}",
                                            "call_id": call_id  # Use original call_id
                                        }
                                    }
                                )

                                await self.rt_client.ws.send_json(
                                    {
                                        "type": "response.create",
                                        "response": {
                                            "modalities": ["text", "audio"],
                                            "instructions": f"Respond to the user that you found named {recipe_name}. Be concise and friendly."
                                        }
                                    }
                                )
                            except Exception as e:
                                logger.error(f"Error in recipe search: {e}")
                                await self.rt_client.ws.send_json(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "output": "Sorry, I encountered an error while searching for recipes.",
                                            "call_id": call_id  # Use original call_id
                                        }
                                    }
                                )
                        elif function_name == "send_recipe":
                            url = args["url"]
                            await self.send_sms(url)
                            pass

                        elif function_name == "transfer_to_agent":
                            # await self.tranfer_call(...)
                            pass

                        logger.info(f"Function Call Arguments: {message.arguments}")
                        print(f"Function Call Arguments: {message.arguments}")
                        pass
                    case _:
                        pass
        except Exception as e:
            logger.error(f"Error in receive_messages_async: {e}")
            if not isinstance(e, asyncio.CancelledError):
                raise e

    async def receive_audio(self, data_payload) -> None:
        try:
            data_payload = {
                "Kind": "AudioData",
                "AudioData": {"Data": data_payload},
                "StopAudio": None,
            }

            # Serialize the server streaming data
            serialized_data = json.dumps(data_payload)
            await self.send_message_async(serialized_data)

        except Exception as e:
            print(e)

    async def send_audio_async(self, audio_data: str) -> None:
        await self.rt_client.send(
            message=InputAudioBufferAppendMessage(
                type="input_audio_buffer.append", audio=audio_data, _is_azure=True
            )
        )

    async def stop_audio_async(self) -> None:
        try:
            stop_audio_data = {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}
            json_data = json.dumps(stop_audio_data)
            await self.send_message_async(json_data)
        except Exception as e:
            # print(f"Stop Audio - Failed to send message: {e}")
            logger.error(f"Stop Audio - Failed to send message: {e}")
            raise e
        return

    async def send_sms(self, message: str) -> None:
        try:
            sms_client = SmsClient.from_connection_string(ACS_SMS_CONNECTION_STRING)
            sms_response_list: list[SmsSendResult] = sms_client.send(
                from_=os.getenv("ACS_SMS_FROM_PHONE_NUMBER"),
                to=[self.target_phone_number],
                message=f"Hello from RecipeFinder! Here's the recipe you requested:\n\n{message}",
            )

            for sms_response in sms_response_list:
                if sms_response.successful is True:
                    logger.info(f"SMS sent: {sms_response}")
                else:
                    logger.error(f"Failed to send SMS: {sms_response}")

        except Exception as e:
            logger.error(f"Failed to send SMS: {e}")
            raise e
