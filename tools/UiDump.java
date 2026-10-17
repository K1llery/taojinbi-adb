import java.lang.reflect.*;
import java.util.List;
import java.util.ArrayList;
import java.util.Base64;

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
        attribute("focused", call(node, nodeType, "isFocused"));
        attribute("password", call(node, nodeType, "isPassword"));
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

    private static void collect(Object node, String type, String text, List<Object> matches, int depth) throws Exception {
        if (node == null) return;
        if (depth > 100 || ++count > 12000) throw new IllegalStateException("Hierarchy exceeds limit");
        if ((type == null || type.contentEquals(String.valueOf(call(node, nodeType, "getClassName")))) &&
            (text == null || text.contentEquals(String.valueOf(call(node, nodeType, "getText")))) &&
            Boolean.TRUE.equals(call(node, nodeType, "isVisibleToUser"))) matches.add(node);
        int children = (Integer) call(node, nodeType, "getChildCount");
        for (int i = 0; i < children; i++)
            collect(nodeType.getMethod("getChild", int.class).invoke(node, i), type, text, matches, depth + 1);
    }

    private static List<Object> matching(Object root, String type, String text) throws Exception {
        count = 0;
        List<Object> matches = new ArrayList<Object>();
        collect(root, type, text, matches, 0);
        return matches;
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
            if (args.length > 0) {
                if (args.length != 2 || !("set-search".equals(args[0]) || "set-web-search".equals(args[0])))
                    throw new IllegalArgumentException("Unsupported reader action");
                if (!"com.taobao.taobao".contentEquals((CharSequence) call(root, nodeType, "getPackageName")))
                    throw new IllegalStateException("Not Taobao");
                List<?> fields;
                if ("set-web-search".equals(args[0])) {
                    List<Object> panels = matching(root, "android.webkit.WebView", "淘宝精选");
                    if (panels.size() != 1) throw new IllegalStateException("Web search panel not unique");
                    Object panel = panels.get(0);
                    if (matching(panel, null, "搜索有福利").size() != 1 ||
                        matching(panel, null, "搜索后浏览立得奖励").size() != 1)
                        throw new IllegalStateException("Web search markers missing");
                    fields = matching(panel, "android.widget.EditText", null);
                } else {
                    fields = (List<?>) nodeType.getMethod("findAccessibilityNodeInfosByViewId", String.class)
                        .invoke(root, "com.taobao.taobao:id/searchEdit");
                }
                if (fields.size() != 1) throw new IllegalStateException("Search field not unique");
                Object field = fields.get(0);
                if (!"android.widget.EditText".contentEquals((CharSequence) call(field, nodeType, "getClassName")) ||
                    !Boolean.TRUE.equals(call(field, nodeType, "isEditable")) ||
                    !Boolean.TRUE.equals(call(field, nodeType, "isVisibleToUser")) ||
                    !Boolean.TRUE.equals(call(field, nodeType, "isEnabled")) ||
                    !Boolean.TRUE.equals(call(field, nodeType, "isFocused")) ||
                    Boolean.TRUE.equals(call(field, nodeType, "isPassword")))
                    throw new IllegalStateException("Search field not ready");
                String query = new String(Base64.getDecoder().decode(args[1]), "UTF-8");
                if (query.length() < 1 || query.length() > 80 || query.matches("(?s).*[\\p{Cntrl}].*"))
                    throw new IllegalArgumentException("Invalid search query");
                Class<?> bundleType = Class.forName("android.os.Bundle");
                Object bundle = bundleType.getConstructor().newInstance();
                bundleType.getMethod("putCharSequence", String.class, CharSequence.class)
                    .invoke(bundle, "ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE", query);
                Object accepted = nodeType.getMethod("performAction", int.class, bundleType)
                    .invoke(field, 2097152, bundle);
                if (!Boolean.TRUE.equals(accepted)) throw new IllegalStateException("Search text action rejected");
                System.out.print("OK");
            } else {
                xml.append("<?xml version=\"1.0\" encoding=\"UTF-8\"?><hierarchy>");
                node(root, 0);
                xml.append("</hierarchy>");
                System.out.print(xml.toString());
            }
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
