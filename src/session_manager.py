import os
import json
import asyncio
import time
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

async def get_fresh_ssid() -> str | None:
    # Run the synchronous Selenium code in a thread so it doesn't block the async loop
    return await asyncio.to_thread(_get_fresh_ssid_sync)

def _get_fresh_ssid_sync() -> str | None:
    email = os.getenv("POCKET_OPTION_EMAIL")
    password = os.getenv("POCKET_OPTION_PASSWORD")
    
    if not email or not password:
        print("Missing POCKET_OPTION_EMAIL or POCKET_OPTION_PASSWORD in environment.")
        return None

    print("Initializing undetected-chromedriver (Stealth Mode)...")
    
    def get_options():
        opts = uc.ChromeOptions()
        opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        user_data_dir = os.path.join(os.getcwd(), "chrome_profile")
        opts.add_argument(f"--user-data-dir={user_data_dir}")
        return opts
    
    driver = None
    ssid = None
    
    try:
        try:
            driver = uc.Chrome(options=get_options())
        except Exception as e:
            if "Current browser version is" in str(e):
                import re
                match = re.search(r"Current browser version is (\d+)", str(e))
                if match:
                    version = int(match.group(1))
                    print(f"Version mismatch. Retrying with Chrome version {version}...")
                    driver = uc.Chrome(options=get_options(), version_main=version)
                else:
                    raise e
            else:
                raise e
                
        driver.set_page_load_timeout(45)
        
        print("Navigating to Pocket Option...")
        driver.get("https://pocketoption.com/en/cabinet/demo-quick-high-low/")
        
        # Give the page a moment to load and evaluate Cloudflare
        time.sleep(5)
        
        # If we are redirected to login, login and then go back to demo
        if "login" in driver.current_url.lower():
            print("Entering credentials...")
            try:
                email_input = driver.find_element(By.CSS_SELECTOR, 'input[type="email"]')
                password_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
                
                email_input.clear()
                email_input.send_keys(email)
                
                password_input.clear()
                password_input.send_keys(password)
                
                print("Clicking SIGN IN...")
                submit_button = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
                submit_button.click()
                
                time.sleep(5)
                print("Navigating to Demo Dashboard...")
                driver.get("https://pocketoption.com/en/cabinet/demo-quick-high-low/")
            except Exception as e:
                print(f"Could not find or submit login form: {e}")
        else:
            print("Already logged in (Persistent Session).")
            
        print("Waiting up to 60 seconds to capture secure Session ID...")
        print("[ACTION] IF YOU SEE A CAPTCHA, PLEASE CLICK IT ON THE SCREEN NOW!")
        
        # Poll the network logs for 60 seconds
        with open("websocket_debug.log", "a", encoding="utf-8") as f:
            for _ in range(60):
                logs = driver.get_log("performance")
                for entry in logs:
                    try:
                        message = json.loads(entry.get("message", "{}")).get("message", {})
                        method = message.get("method")
                        # Look for WebSocket frame received OR sent
                        if method in ["Network.webSocketFrameReceived", "Network.webSocketFrameSent"]:
                            payload_text = message.get("params", {}).get("response", {}).get("payloadData", "")
                            f.write(f"{method}: {payload_text}\n")
                            if payload_text.startswith('42["auth",'):
                                payload_json = json.loads(payload_text[2:])
                                if len(payload_json) > 1 and "session" in payload_json[1]:
                                    ssid = payload_json[1]["session"]
                                    print("[SUCCESS] Successfully intercepted fresh SSID!")
                                    break
                    except Exception:
                        continue
                        
                if ssid:
                    break
                    
                time.sleep(1)
            
        if not ssid:
            print("[ERROR] Failed to capture SSID.")
        else:
            # Save SSID to .env so we don't need Chrome next time
            try:
                from dotenv import set_key
                env_path = os.path.join(os.getcwd(), ".env")
                set_key(env_path, "POCKET_OPTION_SSID", ssid)
                print("[SUCCESS] SSID saved to .env for future sessions.")
            except Exception as e:
                print(f"[ERROR] Could not save SSID to .env: {e}")
            
    except Exception as e:
        print(f"Error during automated login: {e}")
        
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
                
    return ssid

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    result = asyncio.run(get_fresh_ssid())
    print(f"Final SSID: {result}")
