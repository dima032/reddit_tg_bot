from typing import final
import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)    

# constants
TOKEN: final = os.getenv('TELEGRAM_BOT_TOKEN')
BOT_NAME: final = os.getenv('TELEGRAM_BOT_NAME')

import requests

def get_top_posts(subreddit, limit=3):
    url = f"https://www.reddit.com/r/{subreddit}/top.json?limit={limit}&t=day"
    headers = {"User-Agent": "telegram-reddit-bot/0.1"}
    
    try:
        r = requests.get(url, headers=headers, timeout=10) # Added a timeout
        
        # Check if the request was successful
        if r.status_code != 200:
            print(f"Error: Received status code {r.status_code} for subreddit '{subreddit}'")
            return []

        data = r.json()
        
        # Safely access nested keys to avoid crashes
        posts = data.get("data", {}).get("children", [])
        if not posts:
            print(f"No posts found for subreddit '{subreddit}'.")
            return []

        result = []
        for p in posts:
            post_data = p.get("data", {})
            if post_data:
                image_url = post_data.get("url_overridden_by_dest")
                if not image_url or not image_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    image_url = None

                result.append({
                    "title": post_data.get("title", "No Title"),
                    "url": "https://reddit.com" + post_data.get("permalink", ""),
                    "score": post_data.get("score", 0),
                    "image_url": image_url
                })
        return result

    except requests.exceptions.RequestException as e:
        print(f"A network error occurred: {e}")
        return []
    except KeyError as e:
        print(f"Failed to parse JSON, missing key: {e}")
        return []
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return []

# commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('here i\'ll explain how to use the bot')

async def reddit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /reddit <subreddit>")
        return
    subreddit = context.args[0]
    posts = get_top_posts(subreddit)
    
    if not posts:
        await update.message.reply_text(f"Couldn't find any top posts in r/{subreddit}.")
        return

    for p in posts:
        caption = f"🔹 {p['title']}\n👍 {p['score']} — {p['url']}"
        if p['image_url']:
            await update.message.reply_photo(photo=p['image_url'], caption=caption)
        else:
            await update.message.reply_text(caption)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_type: str = update.message.chat.type
    text: str = update.message.text

    print(f'User ({update.message.chat.id}) in {message_type}: "{text}"')

    if message_type == 'group':
        if BOT_NAME in text:
            new_text: str = text.replace(BOT_NAME, '').strip()
            response: str = response_handler(new_text)
        else:
            return
    else:
        response: str = response_handler(text)

    print('Bot', response)
    await update.message.reply_text(response)
async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')


if __name__ == '__main__':
    app = Application.builder().token(TOKEN).build()

    # commands
    app.add_handler(CommandHandler('start', start_command))
    app.add_handler(CommandHandler('reddit', reddit_command))

    # messages
    app.add_handler(MessageHandler(filters.TEXT, message_handler))

    # Errors
    app.add_error_handler(error)

    # polls the bot
    print('Polling...')
    app.run_polling(poll_interval=3)
