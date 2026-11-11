package com.pacer.test.pickerharness;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Bounds and allowlists ActivityTaskManager state before it leaves instrumentation. */
final class ActivityTaskDumpFormatter {
    private static final int MAX_DUMP_CHARS = 65536;
    private static final int MAX_RECORDS = 12;
    private static final int MAX_RECORD_LINES = 512;
    private static final int MAX_RECORD_CHARS = 32768;
    private static final Pattern ACTIVITY_RECORD = Pattern.compile(
            "ActivityRecord\\{[^}]*?\\bu\\d+\\s+([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)\\s+t(\\d+)");
    private static final Pattern LAUNCHED_FROM = Pattern.compile(
            "\\blaunchedFromPackage\\s*=\\s*([^\\s,]+)");
    private static final Pattern RESULT_TO = Pattern.compile(
            "\\b(?:resultTo|mResultTo)\\s*=\\s*(ActivityRecord\\{[^}]*\\}|[^\\s,]+)",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern RESULT_WHO = Pattern.compile(
            "\\b(?:resultWho|mResultWho)\\s*=\\s*([^\\s,]+)", Pattern.CASE_INSENSITIVE);
    private static final Pattern REQUEST_CODE = Pattern.compile(
            "\\b(?:requestCode|mRequestCode|resultCode|mResultCode)\\s*=\\s*(-?\\d+)\\b",
            Pattern.CASE_INSENSITIVE);

    private ActivityTaskDumpFormatter() { }

    static String summarize(String dump, boolean sourceTruncated) {
        String boundedDump = dump == null ? "" : dump.substring(0, Math.min(dump.length(), MAX_DUMP_CHARS));
        boolean truncated = sourceTruncated || dump != null && dump.length() > boundedDump.length();
        String[] lines = boundedDump.split("\\r?\\n");
        List<String> records = new ArrayList<>();
        Set<String> seen = new HashSet<>();

        for (int index = 0; index < lines.length && records.size() < MAX_RECORDS; index++) {
            Matcher activity = ACTIVITY_RECORD.matcher(lines[index]);
            if (!activity.find() || isResultReferenceLine(lines[index])) {
                continue;
            }
            String component = sanitizeComponent(activity.group(1), activity.group(2));
            String taskId = activity.group(3);
            if ("other".equals(component) || !seen.add(component + ";" + taskId)) {
                continue;
            }

            StringBuilder block = new StringBuilder(lines[index]);
            for (int next = index + 1; next < lines.length && next <= index + MAX_RECORD_LINES
                    && block.length() < MAX_RECORD_CHARS; next++) {
                if (lines[next].startsWith("TASK ")) {
                    break;
                }
                if (ACTIVITY_RECORD.matcher(lines[next]).find() && !isResultReferenceLine(lines[next])) {
                    break;
                }
                block.append('\n').append(lines[next]);
            }
            String recordText = block.toString();
            String launchedPackage = capture(LAUNCHED_FROM, recordText, "not-shown");
            if (!isKnownPackage(launchedPackage) && !"not-shown".equals(launchedPackage)) {
                launchedPackage = "other";
            }
            String resultTo = capture(RESULT_TO, recordText, "not-shown");
            resultTo = "not-shown".equals(resultTo) ? resultTo : sanitizeReference(resultTo);
            String resultWho = capture(RESULT_WHO, recordText, "not-shown");
            if (!"not-shown".equals(resultWho)) {
                resultWho = "null".equalsIgnoreCase(resultWho) || "none".equalsIgnoreCase(resultWho)
                        ? "none" : "present";
            }
            String requestCode = capture(REQUEST_CODE, recordText, "not-shown");
            if (!"41".equals(requestCode) && !"73".equals(requestCode)
                    && !"not-shown".equals(requestCode)) {
                requestCode = "other";
            }
            records.add("ACTIVITY_TASK_RECORD component=" + component
                    + ";taskId=" + taskId
                    + ";launchedFromPackage=" + launchedPackage
                    + ";resultTo=" + resultTo
                    + ";resultWho=" + resultWho
                    + ";requestCode=" + requestCode);
        }

        StringBuilder summary = new StringBuilder("ACTIVITY_TASK_SNAPSHOT records=")
                .append(records.size()).append(";truncated=").append(truncated);
        for (String record : records) {
            summary.append("||").append(record);
        }
        return summary.toString();
    }

    private static String capture(Pattern pattern, String value, String fallback) {
        Matcher matcher = pattern.matcher(value);
        return matcher.find() ? matcher.group(1).trim() : fallback;
    }

    private static boolean isResultReferenceLine(String line) {
        return line.matches("(?is).*\\b(?:resultTo|mResultTo)\\s*=.*");
    }

    private static String sanitizeReference(String value) {
        if (value.toLowerCase(java.util.Locale.ROOT).contains("null")) {
            return "none";
        }
        Matcher activity = ACTIVITY_RECORD.matcher(value);
        if (!activity.find()) {
            return "other";
        }
        return sanitizeComponent(activity.group(1), activity.group(2));
    }

    private static String sanitizeComponent(String packageName, String className) {
        if (className.startsWith(".")) {
            className = packageName + className;
        }
        String component = packageName + "/" + className;
        if ("com.android.contacts/com.android.contacts.activities.ContactSelectionActivity".equals(component)
                || "com.pacer.test.probe/com.pacer.test.probe.ProbeActivity".equals(component)
                || "com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity".equals(component)) {
            return component;
        }
        return "other";
    }

    private static boolean isKnownPackage(String packageName) {
        return "com.android.contacts".equals(packageName)
                || "com.pacer.test.probe".equals(packageName)
                || "com.android.contacts.test".equals(packageName);
    }
}
