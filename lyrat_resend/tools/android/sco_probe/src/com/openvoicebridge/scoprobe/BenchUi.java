package com.openvoicebridge.scoprobe;

/**
 * What the generated BenchSpec.build() calls, in the order of the spec (goal 104). BenchActivity makes native widgets
 * from these calls; the PC test of the template language implements it to list the fields. Plain Java.
 */
interface BenchUi {
    void header(String title, String headerTemplate);

    void panel(String id, String title);

    void field(BenchTemplate.Field field);

    void actionRow();

    void action(BenchTemplate.Action action);

    void results();

    void feed(BenchTemplate.Feed feed);
}
