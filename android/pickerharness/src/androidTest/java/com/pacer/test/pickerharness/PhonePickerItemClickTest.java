package com.pacer.test.pickerharness;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.Looper;
import android.util.Base64;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ListAdapter;
import android.widget.ListView;
import android.widget.WrapperListAdapter;

import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.List;

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
    private static final String PROBE_LAUNCHER = "com.pacer.test.pickerharness.ProbeLaunchActivity";
    private static final String PICKER_ACTIVITY = "com.android.contacts.activities.ContactSelectionActivity";
    private static final String PHONE_DATA_ID_ARGUMENT = "fixture_data_id";
    private static final String DISPLAY_NAME_ARGUMENT = "fixture_display_name_b64";
    private static final String PHONE_NUMBER_ARGUMENT = "fixture_phone_b64";
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
        String encodedPhone = arguments.getString(PHONE_NUMBER_ARGUMENT, "");
        String expectedPhoneNumber = new String(
                Base64.decode(encodedPhone, Base64.DEFAULT),
                java.nio.charset.StandardCharsets.UTF_8);
        String normalizedExpectedPhone = normalizePhoneNumber(expectedPhoneNumber);
        assertTrue("fixture Data row id must be a positive decimal integer",
                expectedDataId.matches("[1-9][0-9]{0,18}"));
        assertTrue("fixture display name is missing", !expectedDisplayName.isEmpty());
        assertTrue("fixture phone number is not a valid normalized US number",
                normalizedExpectedPhone.matches("[2-9][0-9]{2}[2-9][0-9]{6}"));

        ActivityLifecycleMonitor lifecycleMonitor = ActivityLifecycleMonitorRegistry.getInstance();
        Intent launcher = new Intent();
        launcher.setClassName("com.android.contacts.test", PROBE_LAUNCHER);
        launcher.putExtra("pacer_test_launch_probe", true);
        launcher.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        instrumentation.getContext().startActivity(launcher);

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
                outcome[0] = tryClickExactRow(resumedPicker, expectedDataId,
                        expectedDisplayName, normalizedExpectedPhone);
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

    private static String normalizePhoneNumber(String value) {
        if (value == null) {
            return "";
        }
        String digits = value.replaceAll("\\D", "");
        if (digits.length() == 11 && digits.startsWith("1")) {
            digits = digits.substring(1);
        }
        return digits;
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
                                                  String expectedDisplayName,
                                                  String normalizedExpectedPhone) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            return new ClickOutcome(false, false, -1, "performItemClick must run on the main UI thread");
        }
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
        if (list.getOnItemClickListener() == null) {
            return new ClickOutcome(false, false, -1, "ListView OnItemClickListener is not bound");
        }
        ListAdapter adapter = list.getAdapter();
        int headerCount = list.getHeaderViewsCount();
        int matchPosition = -1;
        int matchCount = 0;
        boolean phoneIdMatches = false;
        boolean displayNameMatches = false;
        boolean phoneNumberMatches = false;
        boolean hasIdColumn = false;
        boolean hasDisplayNameColumn = false;
        boolean hasPhoneNumberColumn = false;
        // The ListView adapter includes header positions; scan from the first data row.
        for (int position = headerCount; position < adapter.getCount(); position++) {
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
            int phoneColumn = cursor.getColumnIndex(
                    android.provider.ContactsContract.CommonDataKinds.Phone.NUMBER);
            if (phoneColumn < 0) {
                phoneColumn = cursor.getColumnIndex("number");
            }
            hasIdColumn |= idColumn >= 0;
            hasDisplayNameColumn |= nameColumn >= 0;
            hasPhoneNumberColumn |= phoneColumn >= 0;
            if (nameColumn < 0 || phoneColumn < 0) {
                continue;
            }
            boolean phoneIdMatched = idColumn >= 0
                    && expectedDataId.equals(cursor.getString(idColumn));
            boolean displayNameMatched = expectedDisplayName.equals(cursor.getString(nameColumn));
            boolean phoneNumberMatched = normalizedExpectedPhone.equals(
                    normalizePhoneNumber(cursor.getString(phoneColumn)));
            phoneIdMatches |= phoneIdMatched;
            displayNameMatches |= displayNameMatched;
            phoneNumberMatches |= phoneNumberMatched;
            if (phoneNumberMatched && displayNameMatched) {
                matchPosition = position;
                matchCount++;
            }
        }
        if (matchCount == 0) {
            return new ClickOutcome(false, false, -1,
                    "no-match: items=" + adapter.getCount()
                            + "; idCol=" + hasIdColumn + "; nameCol=" + hasDisplayNameColumn
                            + "; phoneCol=" + hasPhoneNumberColumn
                            + "; idMatch=" + phoneIdMatches + "; nameMatch=" + displayNameMatches
                            + "; phoneMatch=" + phoneNumberMatches);
        }
        if (matchCount != 1) {
            return new ClickOutcome(false, false, -1, "fixture data row was not unique");
        }
        if (matchPosition < headerCount) {
            return new ClickOutcome(false, false, matchPosition,
                    "fixture match resolved to a ListView header position");
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
        Instrumentation instrumentation = InstrumentationRegistry.getInstrumentation();
        AdapterView.OnItemClickListener originalListener = list.getOnItemClickListener();
        final boolean[] originalListenerReturned = new boolean[1];
        AdapterView.OnItemClickListener traceListener = (parent, clickedView, position, id) -> {
            int callbackHeaderCount = list.getHeaderViewsCount();
            String uriLookup = describeAdapterDataUri(
                    list.getAdapter(), position, callbackHeaderCount);
            String launchContext = describeActivityLaunchContext(picker);
            sendCallbackTrace(instrumentation, "callback_enter", originalListener.getClass().getName(),
                    position, callbackHeaderCount, uriLookup, "not-read", launchContext);
            try {
                originalListener.onItemClick(parent, clickedView, position, id);
                originalListenerReturned[0] = true;
            } catch (RuntimeException | Error failure) {
                sendCallbackTrace(instrumentation, "callback_throw", originalListener.getClass().getName(),
                        position, callbackHeaderCount, uriLookup, "exception=" + failure.getClass().getName(),
                        "result-snapshot=skipped");
                throw failure;
            }
            sendCallbackTrace(instrumentation, "callback_exit", originalListener.getClass().getName(),
                    position, callbackHeaderCount, uriLookup, inspectPickerActivityResult(picker),
                    "finishing=" + picker.isFinishing() + ";launchContextBeforeCallback=" + launchContext);
        };
        list.setOnItemClickListener(traceListener);
        boolean performItemClickReturned;
        try {
            performItemClickReturned = list.performItemClick(
                    row, matchPosition, list.getItemIdAtPosition(matchPosition));
        } finally {
            list.setOnItemClickListener(originalListener);
        }
        return new ClickOutcome(true, performItemClickReturned && originalListenerReturned[0],
                matchPosition, "clicked; " + inspectPickerActivityResult(picker));
    }

    private static String describeAdapterDataUri(ListAdapter adapter, int listPosition, int headerCount) {
        ListAdapter wrapped = adapter;
        while (wrapped instanceof WrapperListAdapter) {
            wrapped = ((WrapperListAdapter) wrapped).getWrappedAdapter();
        }
        int dataPosition = listPosition - headerCount;
        try {
            Method getDataUri = wrapped.getClass().getMethod("getDataUri", int.class);
            Object value = getDataUri.invoke(wrapped, dataPosition);
            return "lookup=ok;dataPosition=" + dataPosition + ";" + describeUri(value instanceof Uri
                    ? (Uri) value : null);
        } catch (InvocationTargetException failure) {
            Throwable cause = failure.getCause();
            return "lookup=threw;cause=" + (cause == null
                    ? failure.getClass().getSimpleName() : cause.getClass().getName());
        } catch (ReflectiveOperationException | RuntimeException failure) {
            return "lookup=unavailable;cause=" + failure.getClass().getSimpleName();
        }
    }

    private static String describeUri(Uri uri) {
        if (uri == null) {
            return "uriPresent=false";
        }
        List<String> pathSegments = uri.getPathSegments();
        boolean contactDataUri = !pathSegments.isEmpty() && "data".equals(pathSegments.get(0));
        return "uriPresent=true;scheme=" + uri.getScheme()
                + ";authority=" + uri.getAuthority()
                + ";contactDataUri=" + contactDataUri
                + ";pathSegments=" + pathSegments.size();
    }

    private static String describeActivityLaunchContext(Activity activity) {
        android.content.ComponentName caller = activity.getCallingActivity();
        int flags = activity.getIntent() == null ? 0 : activity.getIntent().getFlags();
        return "finishing=" + activity.isFinishing()
                + ";caller=" + (caller == null
                        ? "none" : caller.getPackageName() + "/" + caller.getClassName())
                + ";launchFlags{newTask=" + ((flags & Intent.FLAG_ACTIVITY_NEW_TASK) != 0)
                + ",forwardResult=" + ((flags & Intent.FLAG_ACTIVITY_FORWARD_RESULT) != 0)
                + ",clearTask=" + ((flags & Intent.FLAG_ACTIVITY_CLEAR_TASK) != 0)
                + ",clearTop=" + ((flags & Intent.FLAG_ACTIVITY_CLEAR_TOP) != 0)
                + ",multipleTask=" + ((flags & Intent.FLAG_ACTIVITY_MULTIPLE_TASK) != 0) + "}";
    }

    private static String inspectPickerActivityResult(Activity activity) {
        try {
            Field resultCodeField = Activity.class.getDeclaredField("mResultCode");
            Field resultDataField = Activity.class.getDeclaredField("mResultData");
            resultCodeField.setAccessible(true);
            resultDataField.setAccessible(true);
            int resultCode = resultCodeField.getInt(activity);
            Intent resultData = (Intent) resultDataField.get(activity);
            Uri resultUri = resultData == null ? null : resultData.getData();
            boolean readGrant = resultData != null
                    && (resultData.getFlags() & Intent.FLAG_GRANT_READ_URI_PERMISSION) != 0;
            return "reflection=ok;resultCode=" + resultCode
                    + ";resultIntentPresent=" + (resultData != null)
                    + ";" + describeUri(resultUri)
                    + ";readGrant=" + readGrant;
        } catch (ReflectiveOperationException | RuntimeException failure) {
            return "reflection=unavailable;cause=" + failure.getClass().getSimpleName();
        }
    }

    private static void sendCallbackTrace(Instrumentation instrumentation, String stage,
                                          String listenerClass, int listPosition, int headerCount,
                                          String uriLookup, String resultState, String lifecycleState) {
        Bundle trace = new Bundle();
        trace.putString("contacts_trace_stage", stage);
        trace.putString("contacts_trace_listener", listenerClass);
        trace.putInt("contacts_trace_list_position", listPosition);
        trace.putInt("contacts_trace_header_count", headerCount);
        trace.putString("contacts_trace_adapter_uri", uriLookup);
        trace.putString("contacts_trace_activity_result", resultState);
        trace.putString("contacts_trace_lifecycle", lifecycleState);
        try {
            instrumentation.sendStatus(0, trace);
        } catch (RuntimeException ignored) {
            // Diagnostic transport must not alter Contacts picker behavior.
        }
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
