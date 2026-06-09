import cv2
from pyzbar.pyzbar import decode
import zxingcpp

def read_barcode(image_path):
    # 1. قراءة الصورة
    img = cv2.imread(image_path)
    
    if img is None:
        print("خطأ: مش قادر أفتح الصورة، اتأكد من المسار.")
        return

    # 2. فك تشفير الباركود من الصورة
    barcodes = decode(img)

    # 3. استخراج البيانات
    if not barcodes:
        print("لم يتم العثور على أي باركود في الصورة.")
    else:
        for barcode in barcodes:
            # البيانات بتخرج بصيغة bytes، لازم نحولها لـ string
            barcode_data = barcode.data.decode('utf-8')
            barcode_type = barcode.type
            
            print(f"البيانات: {barcode_data}")
            print(f"النوع: {barcode_type}")
            print("-" * 20)

# استدعاء الدالة (حط مسار صورتك هنا)
#read_barcode('barcode_image3.jpg')


def read_barcode_optimized(image_path):
    # 1. قراءة الصورة
    img = cv2.imread(image_path)
    if img is None:
        print("خطأ في المسار")
        return

    # 2. تحويل الصورة لرمادي (Grayscale)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 3. زيادة التباين (Contrast Enhancement)
    # بنخلي الفاتح أفتح والغامق أغمق عشان الباركود يظهر
    alpha = 1.5 # معامل التباين
    beta = 0    # معامل السطوع
    adjusted = cv2.convertScaleAbs(gray, alpha=alpha, beta=beta)

    # 4. تطبيق Thresholding (تحويلها لأسود وأبيض صريح)
    # ده بيشيل أي "رمادي" في الخلفية ويخلي الباركود واضح جداً
    _, thresh = cv2.threshold(adjusted, 80, 255, cv2.THRESH_BINARY)

    # (اختياري) لو عايز تشوف الصورة بعد التحسين عشان تتأكد
    # cv2.imshow('Optimized Image', thresh)
    # cv2.waitKey(0)

    # 5. فك التشفير
    barcodes = decode(thresh)

    if not barcodes:
        # لو منفعش مع الـ Threshold، نجرب على الـ Adjusted بس
        barcodes = decode(adjusted)

    if not barcodes:
        print("لم يتم العثور على باركود حتى بعد التحسين.")
    else:
        for barcode in barcodes:
            data = barcode.data.decode('utf-8')
            print(f"النوع: {barcode.type} | البيانات: {data}")

#read_barcode_optimized('barcode_image3.jpg')





def read_barcode_final(image_path):
    # 1. قراءة الصورة
    img = cv2.imread(image_path)
    if img is None:
        print("خطأ: المسار غير صحيح")
        return

    # 2. البحث عن الأكواد (الباركود والـ QR)
    # الميزة هنا إننا بنشغل كل محركات البحث في المكتبة
    results = zxingcpp.read_barcodes(img)

    if not results:
        # محاولة أخيرة: تحسين التباين يدوياً قبل البحث
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        enhanced = cv2.equalizeHist(gray) # توزيع الإضاءة بشكل أفضل
        results = zxingcpp.read_barcodes(enhanced)

    if not results:
        print("للأسف، الصورة جودتها ضعيفة جداً للقراءة الآلية.")
    else:
        for result in results:
            print(f"تم العثور على كود!")
            print(f"النوع: {result.format}")
            print(f"المحتوى: {result.text}")
            print("-" * 20)

# جرب على صورتك
#read_barcode_final('barcode_image3.png')


def read_qr_only(image_path):
    # 1. قراءة الصورة
    img = cv2.imread(image_path)
    if img is None:
        print("المسار غير صحيح")
        return

    # 2. تعريف الـ QR Code Detector
    detector = cv2.QRCodeDetector()

    # 3. محاولة استخراج البيانات
    # الدالة دي بتعمل Detect (تحديد مكان) و Decode (قراءة البيانات) في خطوة واحدة
    data, bbox, straight_qrcode = detector.detectAndDecode(img)

    if data:
        print(f"✅ تم العثور على QR Code!")
        print(f"البيانات: {data}")
    else:
        # محاولة أخيرة: تحويل الصورة لرمادي عشان لو الإضاءة وحشة زي صورتك
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        data, bbox, _ = detector.detectAndDecode(gray)
        
        if data:
            print(f"✅ تم العثور على QR Code بعد معالجة الصورة!")
            print(f"البيانات: {data}")
        else:
            print("❌ لم يتم العثور على QR Code. جرب تحسن الإضاءة.")

# شغل الكود على صورتك
read_qr_only('barcode_image3.png')