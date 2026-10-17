import java.lang.reflect.*;

/** One-shot UI hierarchy reader, launched as adb shell; no APK or persistent service. */
public final class UiDump {
    private static Class<?> nodeType;
    private static Class<?> rectType;
    private static final StringBuilder xml = new StringBuilder(128000);
    private static int count;

    private static Object call(Object object, Class<?> type, String method) throws Exception {
        return type.getMethod(method).invoke(object);
    }

    private static String escape(Object value) {
        if (value == null) return "";
        StringBuilder s = new StringBuilder();
        for (char c : value.toString().toCharArray()) {
            if (c == '&') s.append("&amp;");
            else if (c == '<') s.append("&lt;");
            else if (c == '>') s.append("&gt;");
            else if (c == '"') s.append("&quot;");
            else if (c == '\n') s.append("&#10;");
            else if (c == '\r') s.append("&#13;");
            else if (c == '\t') s.append("&#9;");
            else if (c >= 32) s.append(c);
        }
        return s.toString();
    }

    private static void attribute(String name, Object value) {
        xml.append(' ').append(name).append("=\"").append(escape(value)).append('"');
    }

    private static void node(Object node, int depth) throws Exception {
        if (node == null) return;
        if (depth > 100 || ++count > 12000) throw new IllegalStateException("Hierarchy exceeds limit");
        xml.append("<node");
        attribute("text", call(node, nodeType, "getText"));
        attribute("content-desc", call(node, nodeType, "getContentDescription"));
        attribute("package", call(node, nodeType, "getPackageName"));
        attribute("class", call(node, nodeType, "getClassName"));
        attribute("resource-id", call(node, nodeType, "getViewIdResourceName"));
        attribute("clickable", call(node, nodeType, "isClickable"));
        attribute("enabled", call(node, nodeType, "isEnabled"));
        attribute("scrollable", call(node, nodeType, "isScrollable"));
        attribute("visible-to-user", call(node, nodeType, "isVisibleToUser"));
        Object rect = rectType.getConstructor().newInstance();
        nodeType.getMethod("getBoundsInScreen", rectType).invoke(node, rect);
        attribute("bounds", "[" + rectType.getField("left").get(rect) + "," +
                  rectType.getField("top").get(rect) + "][" +
                  rectType.getField("right").get(rect) + "," +
                  rectType.getField("bottom").get(rect) + "]");
        xml.append('>');
        int children = (Integer) call(node, nodeType, "getChildCount");
        for (int i = 0; i < children; i++) {
            node(nodeType.getMethod("getChild", int.class).invoke(node, i), depth + 1);
        }
        xml.append("</node>");
    }

    public static void main(String[] args) {
        Object automation = null;
        Class<?> automationType = null;
        try {
            Class<?> wrapperType = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
            Object wrapper = wrapperType.getConstructor().newInstance();
            wrapperType.getMethod("connect").invoke(wrapper);
            automationType = Class.forName("android.app.UiAutomation");
            automation = wrapperType.getMethod("getUiAutomation").invoke(wrapper);
            Object info = call(automation, automationType, "getServiceInfo");
            Class<?> infoType = Class.forName("android.accessibilityservice.AccessibilityServiceInfo");
            Field flags = infoType.getField("flags");
            flags.setInt(info, flags.getInt(info) | 2 | 16 | 64);
            automationType.getMethod("setServiceInfo", infoType).invoke(automation, info);
            nodeType = Class.forName("android.view.accessibility.AccessibilityNodeInfo");
            rectType = Class.forName("android.graphics.Rect");
            Object root = call(automation, automationType, "getRootInActiveWindow");
            if (root == null) throw new IllegalStateException("No active accessibility window");
            xml.append("<?xml version=\"1.0\" encoding=\"UTF-8\"?><hierarchy>");
            node(root, 0);
            xml.append("</hierarchy>");
            System.out.print(xml.toString());
            automationType.getMethod("disconnect").invoke(automation);
            System.out.flush();
            System.exit(0);
        } catch (Throwable error) {
            if (automation != null && automationType != null) {
                try { automationType.getMethod("disconnect").invoke(automation); } catch (Exception ignored) {}
            }
            error.printStackTrace(System.err);
            System.exit(1);
        }
    }
}
