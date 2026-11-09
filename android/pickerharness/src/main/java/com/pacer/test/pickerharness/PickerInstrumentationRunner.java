package com.pacer.test.pickerharness;

import android.app.Activity;
import android.test.InstrumentationTestRunner;

public final class PickerInstrumentationRunner extends InstrumentationTestRunner {
    private static final String TARGET_ACTIVITY = "com.android.contacts.activities.ContactSelectionActivity";

    private volatile Activity pickerActivity;

    @Override
    public void callActivityOnResume(Activity activity) {
        super.callActivityOnResume(activity);
        if ("com.android.contacts".equals(activity.getPackageName())
                && TARGET_ACTIVITY.equals(activity.getClass().getName())) {
            pickerActivity = activity;
            synchronized (this) {
                notifyAll();
            }
        }
    }

    Activity awaitPickerActivity(long timeoutMillis)
            throws InterruptedException {
        final long deadline = android.os.SystemClock.elapsedRealtime() + timeoutMillis;
        synchronized (this) {
            while (pickerActivity == null) {
                final long remaining = deadline - android.os.SystemClock.elapsedRealtime();
                if (remaining <= 0) {
                    return null;
                }
                wait(remaining);
            }
            return pickerActivity;
        }
    }
}
