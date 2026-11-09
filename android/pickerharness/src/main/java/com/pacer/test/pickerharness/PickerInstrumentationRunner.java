package com.pacer.test.pickerharness;

import android.app.Activity;
import android.os.Bundle;
import android.test.InstrumentationTestRunner;

public final class PickerInstrumentationRunner extends InstrumentationTestRunner {
    private static final String TARGET_ACTIVITY = "com.android.contacts.activities.ContactSelectionActivity";

    private volatile Activity pickerActivity;
    private Bundle runnerArguments = Bundle.EMPTY;

    @Override
    public void onCreate(Bundle arguments) {
        runnerArguments = arguments == null ? Bundle.EMPTY : new Bundle(arguments);
        super.onCreate(arguments);
    }

    Bundle getRunnerArguments() {
        return new Bundle(runnerArguments);
    }

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
