# logger.py

import logging

# Configure the logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Set the logging level to INFO (you can change it to DEBUG, WARNING, etc.)

# Create a console handler to output logs to the console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Create a log formatter to format the log messages
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)

# Add the console handler to the logger
logger.addHandler(console_handler)

def log_message(message: str):
    """
    Function to log a message
    """
    logger.info(message)

def log_error(error_message: str):
    """
    Function to log an error message
    """
    logger.error(error_message)

def log_warning(warning_message: str):
    """
    Function to log a warning message
    """
    logger.warning(warning_message)
