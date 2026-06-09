import os
from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv

load_dotenv()
API = os.getenv("GENAI_API_KEY") # تأكد إنك حطيت الـ API Key في ملف .env بالاسم ده
# إعداد العميل
client = genai.Client(api_key=API)

def check_multiple_images_for_water(image_paths: list[str]) -> str:
    """
    بتبعث قائمة بمسارات الصور، والـ API هيرد بـ JSON يوضح كل صورة وفيها ماية ولا لأ
    """
    try:
        # 1. تجهيز الـ Contents: هنمرر الصور والأسئلة بالترتيب
        contents = []
        
        for i, path in enumerate(image_paths, start=1):
            if os.path.exists(path):
                img = Image.open(path)
                # بنضيف الصورة والتعريف بتاعها للموديل
                contents.append(f"This is Image {i}:")
                contents.append(img)
            else:
                print(f"Warning: Image {path} not found.")

        if not contents:
            return "No valid images provided."

        # بنضيف السؤال العام في الآخر
        contents.append("Look at each numbered image provided and determine if water is present. Respond for each image according to the schema.")

        # 2. تحديد الـ Schema على هيئة Object / Dictionary
        # الرد هيكون حاجة شبه كده: {"image_1": "Yes", "image_2": "No"}
        # بنعرف المفاتيح ديناميكياً بناءً على عدد الصور
        properties_dict = {}
        for i in range(1, len(image_paths) + 1):
            properties_dict[f"image_{i}"] = types.Schema(
                type=types.Type.STRING,
                enum=["Yes", "No"],
                description=f"Result for image number {i}"
            )

        response_schema = types.Schema(
            type=types.Type.OBJECT,
            properties=properties_dict,
            required=[f"image_{i}" for i in range(1, len(image_paths) + 1)]
        )

        # 3. إرسال الطلب
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction="You are a precise quality control assistant. Analyze the images and output JSON strictly mapping each image number to 'Yes' or 'No'.",
                # هنا غيرنا الـ MIME Type لـ JSON عشان يرجع منظّم
                response_mime_type="application/json", 
                response_schema=response_schema
                #temperature=0.0
            )
        )
        
        return response.text.strip()

    except Exception as e:
        return f"Error: {e}"

# --- مثال لتشغيل الكود بمجموعة صور ---
if __name__ == "__main__":
    # قائمة الصور اللي عايز تفحصها مع بعض (تقدير تحط 2، 3، 4 أو أكتر في نفس الطلب)
    images_to_check = ["img1.jpg", "img2.jpg", "img3.jpg", "img4.jpg", "img5.jpg", "img6.jpg", "img7.jpg", "img8.jpg", "img9.jpg"]
    
    # للتجربة: تأكد إن الملفات دي موجودة فعلياً في الفولدر
    print("Sending images to Gemini...")
    json_result = check_multiple_images_for_water(images_to_check)
    
    print("\n--- Final Result (JSON) ---")
    print(json_result)