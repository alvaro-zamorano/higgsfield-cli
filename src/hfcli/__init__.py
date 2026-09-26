"""hf: command-line tool for the Higgsfield API."""

__version__ = "0.1.0"

# Exit codes shared by every command.
EXIT_OK = 0
EXIT_FAILED = 1        # generation did not succeed (failed, nsfw, canceled, API/network error, timeout)
EXIT_USAGE = 2         # bad arguments, invalid params, missing configuration
EXIT_STORAGE = 3       # generation succeeded but download or Cloudinary upload failed
