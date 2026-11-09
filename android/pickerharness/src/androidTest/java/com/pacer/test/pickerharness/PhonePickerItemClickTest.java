package com.pacer.test.pickerharness;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.database.Cursor;
import android.os.Bundle;
import android.util.Base64;
import android.view.View;
import android.widget.ListAdapter;
import android.widget.ListView;

import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitor;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry;
import androidx.test.runner.lifecycle.Stage;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

@RunWith(JUnit4.class)
public final class PhonePickerItemClickTest {
    private static final String PROBE_PACKAGE = "com.pacer.test.probe";
    private static final String PROBE_ACTIVITY = "com.pacer.test.probe.ProbeActivity";
    private static final String PICKER_ACTIVITY = "com.android.contacts.activities.ContactSelectionActivity";
    private static final String PHONE_DATA_ID_ARGUMENT = "fixture_data_id";
    private static final String DISPLAY_NAME_ARGUMENT = "fixture_display_name_b64";
    private static final long LOAD_TIMEOUT_MILLIS = 30000;

    @Test
    public void exactPhoneDataRowUsesListViewPerformItemClick() throws Exception {
        Instrumentation instrumentation = InstrumentationRegistry.getInstrumentation();
        Bundle arguments = InstrumentationRegistry.getArguments();
        String expectedDataId = arguments.getString(PHONE_DATA_ID_ARGUMENT, "");
        String encodedDisplayName = arguments.getString(DISPLAY_NAME_ARGUMENT, "");
        String expectedDisplayName = new String(
                Base64.decode(encodedDisplayName, Base64.DEFAULT),
                java.nio.charset.StandardCharsets.UTF_8);
        assertTrue("fixture Data row id must be a positive decimal integer",
                expectedDataId.matches("[1-9][0-9]{0,18}"));
        assertTrue("fixture display name is missing", !expectedDisplayName.isEmpty());

        ActivityLifecycleMonitor lifecycleMonitor = ActivityLifecycleMonitorRegistry.getInstance();
        Intent probe = new Intent();
        probe.setClassName(PROBE_PACKAGE, PROBE_ACTIVITY);
        probe.putExtra("pacer_test_launch_picker", true);
        probe.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        instrumentation.getContext().startActivity(probe);

        final Activity[] pickerState = new Activity[1];
        final long resumeDeadline = android.os.SystemClock.elapsedRealtime() + LOAD_TIMEOUT_MILLIS;
        while (android.os.SystemClock.elapsedRealtime() < resumeDeadline) {
            instrumentation.runOnMainSync(() -> pickerState[0] = findResumedPicker(lifecycleMonitor));
            if (pickerState[0] != null) {
                break;
            }
            Thread.sleep(250);
        }
        assertNotNull("pinned ContactSelectionActivity did not reach RESUMED", pickerState[0]);
        assertEquals("unexpected package for observed picker", "com.android.contacts",
                pickerState[0].getPackageName());
        assertEquals("unexpected picker activity", PICKER_ACTIVITY, pickerState[0].getClass().getName());

        final ClickOutcome[] outcome = new ClickOutcome[1];
        final long loadDeadline = android.os.SystemClock.elapsedRealtime() + LOAD_TIMEOUT_MILLIS;
        while (android.os.SystemClock.elapsedRealtime() < loadDeadline) {
            instrumentation.runOnMainSync(() -> {
                Activity resumedPicker = findResumedPicker(lifecycleMonitor);
                if (resumedPicker == null) {
                    outcome[0] = new ClickOutcome(false, false, -1,
                            "ContactSelectionActivity is no longer RESUMED");
                    return;
                }
                pickerState[0] = resumedPicker;
                outcome[0] = tryClickExactRow(resumedPicker, expectedDataId, expectedDisplayName);
            });
            if (outcome[0] != null && outcome[0].clicked) {
                break;
            }
            Thread.sleep(250);
        }
        if (outcome[0] == null || !outcome[0].clicked) {
            System.out.println("PICKER_VIEW_DIAGNOSTIC_BEGIN");
            System.out.println("PICKER_ACTIVITY_STATE: " + (pickerState[0] == null
                    ? "no resumed picker activity" : "windowFocus=" + pickerState[0].hasWindowFocus()
                    + "; finishing=" + pickerState[0].isFinishing()));
            if (outcome[0] != null) {
                for (String line : outcome[0].diagnostic.split("\n")) {
                    System.out.println("PICKER_VIEW: " + line);
                }
            }
        }
        assertTrue("fixture row could not be selected through the resumed picker: "
                        + (outcome[0] == null ? "no activity snapshot" : outcome[0].diagnostic),
                outcome[0] != null && outcome[0].clicked);
        System.out.println("PERFORM_ITEM_CLICK target=verified-fixture-data-row; "
                + "position=" + outcome[0].position + "; returned=" + outcome[0].listenerInvoked);
        assertTrue("ListView did not dispatch its item-click listener", outcome[0].listenerInvoked);
    }

    private static Activity findResumedPicker(ActivityLifecycleMonitor lifecycleMonitor) {
        for (Activity activity : lifecycleMonitor.getActivitiesInStage(Stage.RESUMED)) {
            if (PICKER_ACTIVITY.equals(activity.getClass().getName())
                    && "com.android.contacts".equals(activity.getPackageName())
                    && !activity.isFinishing() && !activity.isDestroyed()) {
                return activity;
            }
        }
        return null;
    }

    private static ClickOutcome tryClickExactRow(Activity picker, String expectedDataId,
                                                  String expectedDisplayName) {
        ListView list = picker.findViewById(android.R.id.list);
        if (list == null) {
            View decor = picker.getWindow() == null ? null : picker.getWindow().getDecorView();
            return new ClickOutcome(false, false, -1,
                    "android.R.id.list missing; hierarchy=" + describePickerViews(picker, decor));
        }
        if (!picker.hasWindowFocus()) {
            return new ClickOutcome(false, false, -1, "resumed picker does not own window focus");
        }
        if (!list.isAttachedToWindow() || !list.isShown()) {
            return new ClickOutcome(false, false, -1,
                    "ListView is not attached and shown; hierarchy=" + describePickerViews(picker, list));
        }
        if (list.getAdapter() == null) {
            return new ClickOutcome(false, false, -1,
                    "ListView exists without adapter; hierarchy=" + describePickerViews(picker, list));
        }
        ListAdapter adapter = list.getAdapter();
        int matchPosition = -1;
        int matchCount = 0;
        for (int position = 0; position < adapter.getCount(); position++) {
            Object item = adapter.getItem(position);
            if (!(item instanceof Cursor)) {
                continue;
            }
            Cursor cursor = (Cursor) item;
            int idColumn = cursor.getColumnIndex("_id");
            int nameColumn = cursor.getColumnIndex("display_name");
            if (nameColumn < 0) {
                nameColumn = cursor.getColumnIndex("display_name_primary");
            }
            if (idColumn < 0 || nameColumn < 0) {
                continue;
            }
            if (expectedDataId.equals(cursor.getString(idColumn))
                    && expectedDisplayName.equals(cursor.getString(nameColumn))) {
                matchPosition = position;
                matchCount++;
            }
        }
        if (matchCount == 0) {
            return new ClickOutcome(false, false, -1, "fixture row not loaded");
        }
        if (matchCount != 1) {
            return new ClickOutcome(false, false, -1, "fixture data row was not unique");
        }

        int childIndex = matchPosition - list.getFirstVisiblePosition();
        if (childIndex < 0 || childIndex >= list.getChildCount()) {
            return new ClickOutcome(false, false, matchPosition, "fixture row is not currently visible");
        }
        View row = list.getChildAt(childIndex);
        if (row == null || !row.isEnabled() || !row.isAttachedToWindow() || !row.isShown()) {
            return new ClickOutcome(false, false, matchPosition,
                    "fixture row view is unavailable, hidden, or detached");
        }
        boolean listenerInvoked = list.performItemClick(
                row, matchPosition, list.getItemIdAtPosition(matchPosition));
        return new ClickOutcome(true, listenerInvoked, matchPosition, "clicked");
    }

    private static String describePickerViews(Activity activity, View root) {
        if (root == null) {
            return "window/decor unavailable";
        }
        StringBuilder description = new StringBuilder("windowFocus=")
                .append(activity.hasWindowFocus()).append("; finishing=").append(activity.isFinishing());
        View[] pending = new View[64];
        int pendingCount = 0;
        int visited = 0;
        pending[pendingCount++] = root;
        while (pendingCount > 0 && visited < 32) {
            View view = pending[--pendingCount];
            visited++;
            description.append("\n").append(view.getClass().getSimpleName()).append('#');
            int id = view.getId();
            if (id == View.NO_ID) {
                description.append("no-id");
            } else {
                try {
                    description.append(activity.getResources().getResourceName(id));
                } catch (RuntimeException ignored) {
                    description.append("id");
                }
            }
            description.append(":visibility=").append(view.getVisibility())
                    .append(":enabled=").append(view.isEnabled());
            if (view instanceof android.view.ViewGroup) {
                android.view.ViewGroup group = (android.view.ViewGroup) view;
                description.append(":children=").append(group.getChildCount());
                for (int i = group.getChildCount() - 1;
                     i >= 0 && pendingCount < pending.length; i--) {
                    pending[pendingCount++] = group.getChildAt(i);
                }
            }
        }
        return description.toString();
    }

    private static final class ClickOutcome {
        final boolean clicked;
        final boolean listenerInvoked;
        final int position;
        final String diagnostic;

        ClickOutcome(boolean clicked, boolean listenerInvoked, int position, String diagnostic) {
            this.clicked = clicked;
            this.listenerInvoked = listenerInvoked;
            this.position = position;
            this.diagnostic = diagnostic;
        }
    }
}
