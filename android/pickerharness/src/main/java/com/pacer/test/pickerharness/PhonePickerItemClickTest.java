package com.pacer.test.pickerharness;

import android.app.Activity;
import android.content.Intent;
import android.database.Cursor;
import android.os.Bundle;
import android.test.InstrumentationTestCase;
import android.util.Base64;
import android.view.View;
import android.widget.ListAdapter;
import android.widget.ListView;

public final class PhonePickerItemClickTest extends InstrumentationTestCase {
    private static final String PROBE_PACKAGE = "com.pacer.test.probe";
    private static final String PROBE_ACTIVITY = "com.pacer.test.probe.ProbeActivity";
    private static final String PICKER_ACTIVITY = "com.android.contacts.activities.ContactSelectionActivity";
    private static final String PHONE_DATA_ID_ARGUMENT = "fixture_data_id";
    private static final String DISPLAY_NAME_ARGUMENT = "fixture_display_name_b64";
    private static final long LOAD_TIMEOUT_MILLIS = 30000;

    public void testExactPhoneDataRowUsesListViewPerformItemClick() throws Exception {
        PickerInstrumentationRunner runner = (PickerInstrumentationRunner) getInstrumentation();
        Bundle arguments = runner.getRunnerArguments();
        String expectedDataId = arguments.getString(PHONE_DATA_ID_ARGUMENT, "");
        String encodedDisplayName = arguments.getString(DISPLAY_NAME_ARGUMENT, "");
        String expectedDisplayName = new String(
                Base64.decode(encodedDisplayName, Base64.DEFAULT),
                java.nio.charset.StandardCharsets.UTF_8);
        assertTrue("fixture Data row id must be a positive decimal integer",
                expectedDataId.matches("[1-9][0-9]{0,18}"));
        assertTrue("fixture display name is missing", !expectedDisplayName.isEmpty());

        Activity stale = runner.awaitPickerActivity(1);
        assertNull("unexpected picker activity already observed before this run", stale);

        Intent probe = new Intent();
        probe.setClassName(PROBE_PACKAGE, PROBE_ACTIVITY);
        probe.putExtra("pacer_test_launch_picker", true);
        probe.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getInstrumentation().getContext().startActivity(probe);

        Activity picker = runner.awaitPickerActivity(LOAD_TIMEOUT_MILLIS);
        assertNotNull("pinned ContactSelectionActivity did not resume", picker);
        assertEquals("unexpected package for observed picker", "com.android.contacts",
                picker.getPackageName());
        assertEquals("unexpected picker activity", PICKER_ACTIVITY, picker.getClass().getName());

        final ClickOutcome[] outcome = new ClickOutcome[1];
        final long deadline = android.os.SystemClock.elapsedRealtime() + LOAD_TIMEOUT_MILLIS;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            getInstrumentation().runOnMainSync(() -> {
                outcome[0] = tryClickExactRow(picker, expectedDataId, expectedDisplayName);
            });
            if (outcome[0] != null && outcome[0].clicked) {
                break;
            }
            Thread.sleep(250);
        }
        assertNotNull("picker ListView or adapter was not ready before the bounded deadline", outcome[0]);
        assertTrue("fixture row was not uniquely present and visible: " + outcome[0].diagnostic,
                outcome[0].clicked);
        System.out.println("PERFORM_ITEM_CLICK target=verified-fixture-data-row; "
                + "position=" + outcome[0].position + "; returned=" + outcome[0].listenerInvoked);
        assertTrue("ListView did not dispatch its item-click listener", outcome[0].listenerInvoked);
    }

    private static ClickOutcome tryClickExactRow(Activity picker, String expectedDataId,
                                                  String expectedDisplayName) {
        ListView list = picker.findViewById(android.R.id.list);
        if (list == null || list.getAdapter() == null) {
            return null;
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

        int firstVisible = list.getFirstVisiblePosition();
        int childIndex = matchPosition - firstVisible;
        if (childIndex < 0 || childIndex >= list.getChildCount()) {
            return new ClickOutcome(false, false, matchPosition, "fixture row is not currently visible");
        }
        View row = list.getChildAt(childIndex);
        if (row == null || !row.isEnabled()) {
            return new ClickOutcome(false, false, matchPosition, "fixture row view is unavailable or disabled");
        }
        boolean listenerInvoked = list.performItemClick(
                row, matchPosition, list.getItemIdAtPosition(matchPosition));
        return new ClickOutcome(true, listenerInvoked, matchPosition, "clicked");
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
