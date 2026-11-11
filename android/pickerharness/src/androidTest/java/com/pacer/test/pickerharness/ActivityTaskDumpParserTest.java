package com.pacer.test.pickerharness;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

@RunWith(JUnit4.class)
public final class ActivityTaskDumpParserTest {
    @Test
    public void api35ActivityRecordsExposeResultLinkageAndRedactIntentData() {
        String api35Dump = "TASK 9:com.pacer.test.probe id=9 userId=0\n"
                + "  * Hist #2: ActivityRecord{a1 u0 com.android.contacts/.activities.ContactSelectionActivity t9}\n"
                + "    packageName=com.android.contacts processName=com.android.contacts\n"
                + "    launchedFromPackage=com.pacer.test.probe\n"
                + "    resultTo=ActivityRecord{b2 u0 com.pacer.test.probe/.ProbeActivity t9} resultWho=null resultCode=41\n"
                + "    Intent { act=android.intent.action.PICK dat=content://private/secret/123456 }\n"
                + "  * Hist #1: ActivityRecord{b2 u0 com.pacer.test.probe/.ProbeActivity t9}\n"
                + "    launchedFromPackage=com.android.contacts.test\n"
                + "    resultTo=ActivityRecord{c3 u0 com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity t9} resultWho=null resultCode=73\n"
                + "    Intent { dat=content://private/probe-secret }\n"
                + "  * Hist #0: ActivityRecord{c3 u0 com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity t9}\n"
                + "    mResultTo=ActivityRecord{b2 u0 com.pacer.test.probe/.ProbeActivity t9} mResultWho=null mRequestCode=73\n"
                + "TASK 10:unrelated.app id=10 userId=0\n"
                + "  * Hist #0: ActivityRecord{d4 u0 unrelated.app/.PrivateActivity t10}\n"
                + "    mRequestCode=999\n";

        int resultLine = api35Dump.indexOf("    resultTo=ActivityRecord{b2");
        StringBuilder expandedDump = new StringBuilder(api35Dump.substring(0, resultLine));
        for (int index = 0; index < 32; index++) {
            expandedDump.append("    activityDetailField=").append(index).append('\n');
        }
        expandedDump.append(api35Dump.substring(resultLine));
        String summary = ActivityTaskDumpFormatter.summarize(expandedDump.toString(), false);
        System.out.println("ACTIVITY_TASK_PARSER_SUMMARY " + summary);

        assertTrue("API 35 record count: " + summary, summary.contains("records=3;truncated=false"));
        assertTrue("Contacts result target/code: " + summary, summary.contains(
                "component=com.android.contacts/com.android.contacts.activities.ContactSelectionActivity"
                        + ";taskId=9;launchedFromPackage=com.pacer.test.probe"
                        + ";resultTo=com.pacer.test.probe/com.pacer.test.probe.ProbeActivity"
                        + ";resultWho=none;requestCode=41"));
        assertTrue("Probe result target/code", summary.contains(
                "component=com.pacer.test.probe/com.pacer.test.probe.ProbeActivity"
                        + ";taskId=9;launchedFromPackage=com.android.contacts.test"
                        + ";resultTo=com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity"
                        + ";resultWho=none;requestCode=73"));
        assertTrue("wrapper alias result target/code", summary.contains(
                "component=com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity"
                        + ";taskId=9;launchedFromPackage=not-shown;resultTo=com.pacer.test.probe/com.pacer.test.probe.ProbeActivity"
                        + ";resultWho=none;requestCode=73"));
        assertFalse("raw intent label escaped formatter", summary.contains("Intent"));
        assertFalse("raw URI escaped formatter", summary.contains("content://"));
        assertFalse("private fixture text escaped formatter", summary.contains("secret"));
        assertFalse("private numeric identifier escaped formatter", summary.contains("123456"));
        assertFalse("unrelated component escaped allowlist", summary.contains("unrelated.app"));
        assertFalse("unrelated request code escaped allowlist", summary.contains("999"));
    }
}
