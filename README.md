# TeekaGramAi - Installation Guide (⊙o⊙)

This guide will help you install and configure the program from scratch (≧∇≦)ﾉ

## Basic Dependencies
### Step 1: Installing Python

To run the program, Python version 3.8 or newer is required

1.  Go to the official website [python.org](https://www.python.org/downloads/) and download the latest version of Python
2.  Run the installer
3.  **Very important:** On the first installation screen, be sure to check the box **"Add Python to PATH"**. This will allow you to run Python from any folder in the command line, it's impossible to proceed without it! 

   ![Image](https://github.com/user-attachments/assets/ae4956be-3e92-4477-b889-cb607af0a4dc)
   
4.  Follow the on-screen instructions until the installation is complete[]~(￣▽￣)~*
   (Wherever there is something related to installing pip, always check the box to say yes, install it.[Here is a guide on YouTube to make it easier for you to install](https://www.youtube.com/watch?v=82qj_kXQpuo))

5.  To check that everything is installed correctly, open the command line (or terminal) and run two commands:

    ```bash
    python --version
    pip --version
    ```

    Each command should output its version without errors.
#### **How to open the command line (terminal)?**

All further commands will need to be entered into a special window. Here is how to open it:

*   Press the `Win` key (the button with a flag on your keyboard), start typing `cmd`, and select the found application.

   ![Image](https://github.com/user-attachments/assets/158bdaf6-eaba-4b6c-82a4-3b63832e1db9)
   
### Step 2: Downloading the program

1.  You need to download all the program files. The easiest way is to clone the repository using Git. If you don't have Git installed, you can download it [here](https://git-scm.com/downloads)

    ```bash
    git clone https://github.com/biolog-end/TeekaGramAi
    ```

2.  If you don't have Git, you can simply download all the files as a ZIP archive and extract it to a folder convenient for you ^^

   ![image](https://github.com/user-attachments/assets/88b3961e-1967-4a44-abad-d32bac6a4654)

### Step 3: Installing dependencies

1.  Open the folder where you downloaded the program, and open a console in it (you can type cmd in the search bar and press Enter, after which the console will open)

   ![image](https://github.com/user-attachments/assets/53e5bdac-2013-4f34-8ab3-29fcd7c00fa7)

2.  Now let's install all the necessary libraries using a single command. This command will install all packages directly into your main Python environment

    ```bash
    pip install -r requirements.txt
    ```

    Wait for the installation of all packages to complete.

## Steps specific to this project
## Step 4: Creating required files and folders

Inside the project folder, you need to create the following structure:

1.  Open the `data` folder
2.  Inside the `data` folder, rename the file `accountsExample.json` to `accounts.json`. It is needed to select a Telegram account at startup. Configure the names of the accounts you want to have and their quantity in it:

    ```json
    {
      "Account name 1": "accounts/account1.session",
      "Account name 2": "accounts/account2.session"
    }
    ```
    *   `"Account name 1"` - this is any convenient name for you to display in the console
    *   `"account1"` - this is the name of the session file that will be created automatically. Use only Latin letters without spaces; only the program will see it, so it doesn't really matter

## Step 5: Getting API keys

To run the program, you need two sets of keys: from Telegram and from Google

### 5.1. Telegram API

1.  Go to [my.telegram.org](https://my.telegram.org) and log in to your Telegram account 
2.  After logging in, click on **"API development tools"**
3.  Fill out the short form "Create new application" 
4. `App title` and `Short name` can be anything, for example, `MyBrother` and `mybro42`. `URL` also doesn't matter, you can write  
   ```txt
	http://localhost/
    ```
    For `Platform` it's better to choose `Desktop`, `Description` can be left empty
	
    ![image](https://github.com/user-attachments/assets/cd380f09-eb9b-4ae6-9a0c-307e34a7b0b6)
	
5.  After creating the application, you will see your keys: **`api_id`** and **`api_hash`**. Copy them, we will need them a bit later
   (The data in the image is fake)
   
   ![image](https://github.com/user-attachments/assets/7e0ab341-bc58-4909-8777-59d3b32cdff5)

### 5.2. Google Gemini API

1.  Go to the [Google AI Studio](https://aistudio.google.com/) website
2.  Log in to your Google account
3.  Click on the **"Get API key"** button in the bottom left corner
4.  Click **"Create API key"**
5.  Copy the generated key and make sure to save it somewhere! 

## Step 6: Configuration

Now you need to pass the obtained keys into the program.

1.  In the root folder of the project (where `main.py` is located), create a file named `.env`
2.  Open it and copy the following text into it, inserting your keys instead of `...`:

    **File: `.env`**
    ```env
    # --- Telegram Keys ---
    TELAGRAMM_API_ID="..."
    TELAGRAMM_API_HASH="..."

    # --- Google Gemini Key ---
    GOOGLE_API_KEY="..."

    # Instance number if you are running multiple copies. Changes the web interface port
    INSTANCE_NUMBER=1
    ```

## Step 7: First Launch

Everything is ready for the first launch! ヾ(≧▽≦*)o

1.  Run the program from the project folder with the command:

    ```bash
    python main.py
    ```

2.  During the first launch, the following will happen in the console:
    *   The program will ask you to select an account from those you specified in `data/accounts.json`. Enter the number and press Enter
    *   Next, the program will ask you to authorize: enter your phone number, the code from Telegram, and, if necessary, your two-factor authentication password

3.  A message will appear in the console stating that the web server is running. Usually, it is available at `http://127.0.0.1:5001` (the port depends on `INSTANCE_NUMBER` in the `.env` file)
4.  Open this link in your browser to start working

**Installation complete! 🤑(∩^o^)⊃━☆**
